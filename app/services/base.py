"""Shared service behaviour: transactions, conflict translation, activity log, CRUD."""

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, TypeVar

import psycopg.errors
from sqlalchemy import ColumnElement, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.db.base import Base
from app.models.activity import ActivityLog
from app.models.user import AppUser
from app.repositories.base import ModelT, Repository, order_by_clause, text_search
from app.schemas.common import ListQuery

_CONSTRAINT_MESSAGES = {
    "uq_properties_code": "Another property already uses that code.",
    "uq_buildings_property_name": "This property already has a building with that name.",
    "uq_floors_building_level": "This building already has a floor at that level.",
    "uq_layouts_property_name": "This property already has a layout with that name.",
    "uq_spaces_property_code": "This property already has a space with that code.",
    "uq_spaces_floor_root": "This floor already has a whole-floor space.",
    "fk_spaces_parent_space_id_spaces": (
        "The space hierarchy changed while this request was running. Try again."
    ),
    "uq_property_amenities_pair": "This amenity is already assigned to the property.",
    "uq_assets_asset_tag": "Another asset already uses that asset tag.",
    "uq_amenities_name": "An amenity with that name already exists.",
    "ex_tenant_occupancies_space_dates": (
        "The space is already occupied by another tenant during those dates."
    ),
    "ex_bookings_space_time": "The space is already booked for part of that time.",
    "ex_allocation_claims_exclusive": (
        "The space is already booked or contracted for part of that time."
    ),
    "ex_allocation_claims_mixed": (
        "The space, or a space containing it or inside it, is already booked or contracted "
        "for part of that time."
    ),
    "ck_allocation_claim_chain": (
        "The space hierarchy changed while this request was running. Nothing was saved; try again."
    ),
    "ex_pricing_rules_active_overlap": (
        "An active price for the same space (or default), basis and billing period already "
        "covers part of those dates. Retire it, or end it before this one starts."
    ),
    "ck_spaces_move_blocked": (
        "The space, or a space inside it, has active or future bookings or contracts, so it "
        "cannot be moved."
    ),
    "ck_spaces_change_blocked": (
        "The space, or a space inside it, has active or future bookings or contracts, so it "
        "cannot be archived."
    ),
}


def _conflict_message(exc: IntegrityError) -> str:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint in _CONSTRAINT_MESSAGES:
        return _CONSTRAINT_MESSAGES[constraint]
    if isinstance(exc.orig, psycopg.errors.ForeignKeyViolation):
        return "This record is linked to other records, so the change cannot be made."
    return "The change conflicts with existing data."


def flush(session: Session) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(_conflict_message(exc)) from None


# Set by ``one_transaction``: services then flush where they would commit.
_DEFER_COMMIT = "defer_commit"


@contextmanager
def one_transaction(session: Session) -> Iterator[None]:
    """Run several service calls as one transaction: each one's commit becomes a flush,
    and the caller commits (or rolls back) everything once at the end."""
    session.info[_DEFER_COMMIT] = True
    try:
        yield
    finally:
        session.info.pop(_DEFER_COMMIT, None)


def commit(session: Session) -> None:
    """Commit, turning constraint violations into 409 responses without SQL details."""
    if session.info.get(_DEFER_COMMIT):
        flush(session)
        return
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(_conflict_message(exc)) from None


def log_activity(
    session: Session,
    actor: AppUser | None,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    property_id: uuid.UUID | None,
    message: str,
    at: datetime | None = None,
) -> None:
    """Record a change in the same transaction as the change itself.

    ``at`` stamps the entry with the moment of the step instead of the transaction's
    start, which keeps several steps of one transaction in order."""
    entry = ActivityLog(
        actor_id=actor.id if actor else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        property_id=property_id,
        message=message[:500],
    )
    if at is not None:
        entry.created_at = at
    session.add(entry)


# Upper bound on rows loaded when a list is filtered by a derived (computed) value.
DERIVED_FILTER_LIMIT = 5000

ItemT = TypeVar("ItemT")


def page_of(items: list[ItemT], query: ListQuery) -> tuple[list[ItemT], int]:
    """Slice an already-filtered, already-sorted list into the requested page."""
    start = (query.page - 1) * query.page_size
    return items[start : start + query.page_size], len(items)


def require(session: Session, model: type[ModelT], record_id: uuid.UUID, label: str) -> ModelT:
    record = Repository(session, model).get(record_id)
    if record is None:
        raise NotFoundError(f"{label} not found.")
    return record


class CrudService(Generic[ModelT]):
    """Create/read/update/delete for one model. Subclasses add validation rules."""

    model: ClassVar[type[Base]]
    label: ClassVar[str]
    search_fields: ClassVar[tuple[str, ...]] = ()
    sort_fields: ClassVar[tuple[str, ...]] = ("created_at",)
    default_sort: ClassVar[str] = "-created_at"

    def __init__(self, session: Session, actor: AppUser | None = None) -> None:
        self.session = session
        self.actor = actor
        self.repository: Repository[ModelT] = Repository(session, self.model)  # type: ignore[arg-type]

    # --- hooks -----------------------------------------------------------

    def validate(self, values: dict[str, Any], existing: ModelT | None) -> None:
        """Check business rules against the full set of values being saved."""

    def before_delete(self, record: ModelT) -> None:
        """Raise ConflictError if the record must not be deleted or archived."""

    def keep_for_history(self, record: ModelT) -> bool:
        """True if an archivable record must be archived even when nothing references it."""
        return False

    def describe(self, record: ModelT) -> str:
        return str(getattr(record, "name", record.id))  # type: ignore[attr-defined]

    def property_id_of(self, record: ModelT) -> uuid.UUID | None:
        return getattr(record, "property_id", None)

    # --- operations ------------------------------------------------------

    def list(
        self,
        query: ListQuery,
        filters: dict[str, Any] | None = None,
        *,
        include_archived: bool = False,
        conditions: Sequence[ColumnElement[bool]] = (),
    ) -> tuple[list[ModelT], int]:
        statement = self.repository.select(include_archived=include_archived).where(*conditions)
        for field, value in (filters or {}).items():
            if value is not None:
                statement = statement.where(getattr(self.model, field) == value)
        if query.search and self.search_fields:
            columns = [getattr(self.model, field) for field in self.search_fields]
            statement = statement.where(text_search(columns, query.search))
        allowed = {field: getattr(self.model, field) for field in self.sort_fields}
        order = order_by_clause(query.sort, allowed, self.default_sort)
        # The id tiebreaker keeps pages stable when sort values repeat.
        statement = statement.order_by(*order, self.model.id)  # type: ignore[attr-defined]
        return self.repository.paginate(statement, query)

    def get(self, record_id: uuid.UUID) -> ModelT:
        record = self.repository.get(record_id)
        if record is None:
            raise NotFoundError(f"{self.label} not found.")
        return record

    def create(self, values: dict[str, Any]) -> ModelT:
        self.validate(values, None)
        record = self.model(**values)
        if hasattr(record, "created_by") and self.actor is not None:
            record.created_by = self.actor.id
        self.repository.add(record)  # type: ignore[arg-type]
        flush(self.session)
        self._log("created", record)  # type: ignore[arg-type]
        commit(self.session)
        return record  # type: ignore[return-value]

    def update(self, record_id: uuid.UUID, values: dict[str, Any]) -> ModelT:
        record = self.get(record_id)
        if not values:
            return record
        current = {
            attribute.key: getattr(record, attribute.key)
            for attribute in inspect(self.model).column_attrs
        }
        self.validate(current | values, record)
        for field, value in values.items():
            setattr(record, field, value)
        self._log("updated", record)
        commit(self.session)
        return record

    def delete(self, record_id: uuid.UUID) -> str:
        """Delete the record. An archivable record that anything still refers to (bookings,
        leases, prices, files, spaces inside it...) is archived instead, so history stays
        intact. Returns "DELETED" or "ARCHIVED"."""
        record = self.get(record_id)
        self.before_delete(record)
        if not hasattr(record, "archived_at"):
            self._log("deleted", record)
            self.repository.delete(record)
            commit(self.session)
            return "DELETED"
        if not self.keep_for_history(record):
            try:
                # The database knows every reference; a refused delete means it is in use.
                with self.session.begin_nested():
                    # Logged first: the entry may point at the record until it is gone.
                    self._log("deleted", record)
                    self.session.flush()
                    self.repository.delete(record)
                    self.session.flush()
            except IntegrityError:
                record = self.get(record_id)
            else:
                commit(self.session)
                return "DELETED"
        record.archived_at = datetime.now(UTC)
        self._log("archived", record)
        commit(self.session)
        return "ARCHIVED"

    def _log(self, action: str, record: ModelT) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type=self.model.__tablename__,
            entity_id=record.id,  # type: ignore[attr-defined]
            property_id=self.property_id_of(record),
            message=f'{self.label} "{self.describe(record)}" was {action}.',
        )
