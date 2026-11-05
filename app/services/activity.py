"""The admin activity history (Phase 2e, BR-033), read-only over the existing activity log.

Every entry is shown with its stored message unchanged. What is added is looked up now and
labelled as such: readable labels, the actor, the property's current name, whether the
record still exists, and its current building/floor/space ("current context", which may
differ from when the entry was written).

Before/after values are read only from the two exact Phase 2c reimbursement formats that
write them; every other entry has no field-level changes, and none are inferred.

A page takes a fixed number of queries whatever its size: the page and its count, then one
query per kind of record on it, plus actors, properties and building/floor/space names.
"""

import re
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError
from app.db.base import Base
from app.models.account import Account
from app.models.activity import ActivityLog
from app.models.agreement import ContractDocument, ContractTemplate
from app.models.amenity import PropertyAmenity
from app.models.asset import Asset
from app.models.booking import Booking
from app.models.building import Building, Floor
from app.models.crm import Lead, LeadImportBatch
from app.models.file import StoredFile
from app.models.layout import Layout
from app.models.meta import MetaLeadForm
from app.models.pricing import BillingProfile, PricingRule
from app.models.property import Property
from app.models.reimbursement import (
    ReimbursementExpense,
    ReimbursementExpenseType,
    ReimbursementTerm,
)
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.repositories.base import paginate_rows, text_search
from app.schemas.activity import (
    ActivityActor,
    ActivityItem,
    ActivityOption,
    ActivityOptions,
    ActivityRecord,
    CurrentContext,
    RecordedChange,
)
from app.schemas.common import ListQuery

ENTITY_LABELS = {
    "properties": "Property",
    "buildings": "Building",
    "floors": "Floor",
    "spaces": "Space",
    "layouts": "Layout",
    "assets": "Asset",
    "tenants": "Tenant",
    "bookings": "Booking",
    "contracts": "Contract",
    "tenant_occupancies": "Contract",
    "pricing_rules": "Price rule",
    "billing_profiles": "Billing profile",
    "files": "File",
    "property_amenities": "Amenity",
    "reimbursement_expense_types": "Expense type",
    "reimbursement_terms": "Reimbursement term",
    "reimbursement_expenses": "Reimbursement expense",
    "contract_templates": "Contract template",
    "contract_documents": "Contract document",
    "leads": "Lead",
    "lead_imports": "Lead import",
    "meta_lead_forms": "Meta lead form",
    "accounts": "Account",
}
ACTION_LABELS = {
    "created": "Created",
    "updated": "Updated",
    "deleted": "Deleted",
    "archived": "Archived",
    "moved": "Moved",
    "retired": "Retired",
    "cancelled": "Cancelled",
    "confirmed": "Confirmed",
    "converted": "Converted",
    "rescheduled": "Rescheduled",
    "requested": "Requested",
    "approved": "Approved",
    "rejected": "Rejected",
    "activated": "Activated",
    "extended": "Extended",
    "terminated": "Terminated",
    "uploaded": "Uploaded",
    "generated": "Generated",
    "reviewed": "Reviewed",
    "client_invited": "Client app invitation sent",
    "client_activated": "Client app activated",
    "password_changed": "Password changed",
    "stage_changed": "Stage changed",
    "interest_added": "Interest added",
    "interest_removed": "Interest removed",
    "note_added": "Note added",
    "note_updated": "Note edited",
    "note_deleted": "Note deleted",
    "task_added": "Follow-up added",
    "task_updated": "Follow-up updated",
    "task_completed": "Follow-up completed",
    "task_reopened": "Follow-up reopened",
    "imported": "Imported",
    "source_changed": "Source changed",
    "synced": "Synced",
    "opened": "Opened",
    "task_deleted": "Follow-up deleted",
}
# The record each kind of entry points at, to see whether it still exists.
MODELS: dict[str, type[Base]] = {
    "properties": Property,
    "buildings": Building,
    "floors": Floor,
    "spaces": Space,
    "layouts": Layout,
    "assets": Asset,
    "tenants": Tenant,
    "bookings": Booking,
    "contracts": TenantOccupancy,
    "tenant_occupancies": TenantOccupancy,
    "pricing_rules": PricingRule,
    "billing_profiles": BillingProfile,
    "files": StoredFile,
    "property_amenities": PropertyAmenity,
    "reimbursement_expense_types": ReimbursementExpenseType,
    "reimbursement_terms": ReimbursementTerm,
    "reimbursement_expenses": ReimbursementExpense,
    "contract_templates": ContractTemplate,
    "contract_documents": ContractDocument,
    "leads": Lead,
    "lead_imports": LeadImportBatch,
    "meta_lead_forms": MetaLeadForm,
    "accounts": Account,
}
NAME_FIELDS = ("name", "reference", "original_name")

# The two Phase 2c messages that record before/after values, and the fields they may name.
_RECORDED_FORMATS: dict[tuple[str, str], tuple[re.Pattern[str], frozenset[str]]] = {
    ("reimbursement_expense_types", "updated"): (
        re.compile(r'^Expense type "[^"]*" updated: (?P<changes>.+)\.$', re.DOTALL),
        frozenset({"name", "description", "is_active"}),
    ),
    ("reimbursement_expenses", "updated"): (
        re.compile(r"^Expense corrected before review: (?P<changes>.+)\.$", re.DOTALL),
        frozenset(
            {
                "period_start",
                "period_end",
                "amount",
                "description",
                "tax_treatment",
                "tax_rate",
                "receipt_file_id",
            }
        ),
    ),
}
_ARROW = " -> "


def _humanize(value: str) -> str:
    text = value.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else value


def entity_label(entity_type: str) -> str:
    return ENTITY_LABELS.get(entity_type, _humanize(entity_type))


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, _humanize(action))


def recorded_changes(entity_type: str, action: str, message: str) -> list[RecordedChange]:
    """Before/after values from the two exact reimbursement formats; otherwise none. All
    or nothing: if any part is not exactly "<known field> <old> -> <new>", nothing is
    returned, so an ambiguous message is shown only as stored. A message cut at 500
    characters no longer ends with the full stop, so it is not read either."""
    known = _RECORDED_FORMATS.get((entity_type, action))
    if known is None or len(message) >= 500:
        return []
    pattern, fields = known
    match = pattern.match(message)
    if match is None:
        return []
    changes = []
    for part in match.group("changes").split("; "):
        field, _, rest = part.partition(" ")
        if field not in fields or rest.count(_ARROW) != 1:
            return []
        before, after = rest.split(_ARROW)
        changes.append(RecordedChange(field=field, before=before, after=after))
    return changes


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(get_settings().business_timezone))


class ActivityHistory:
    def __init__(self, session: Session) -> None:
        self.session = session

    def page(
        self,
        query: ListQuery,
        *,
        property_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        entity_id: uuid.UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[list[ActivityItem], int]:
        if date_from and date_to and date_to < date_from:
            raise BadRequestError("date_to cannot be before date_from.")
        where: list[ColumnElement[bool]] = []
        for column, value in (
            (ActivityLog.property_id, property_id),
            (ActivityLog.entity_type, entity_type),
            (ActivityLog.action, action),
            (ActivityLog.actor_id, actor_id),
            (ActivityLog.entity_id, entity_id),
        ):
            if value is not None:
                where.append(column == value)
        # Calendar days in the business time zone, both ends included.
        if date_from:
            where.append(ActivityLog.created_at >= _day_start(date_from))
        if date_to:
            where.append(ActivityLog.created_at < _day_start(date_to + timedelta(days=1)))
        if query.search:
            where.append(text_search([ActivityLog.message], query.search))
        statement = (
            select(ActivityLog)
            .where(*where)
            # Newest first; the id breaks ties so pages never overlap or skip.
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        )
        rows, total = paginate_rows(self.session, statement, query)
        return self._items([row[0] for row in rows]), total

    def _items(self, entries: list[ActivityLog]) -> list[ActivityItem]:
        if not entries:
            return []
        actors = {
            u.id: u
            for u in self.session.scalars(
                select(AppUser).where(AppUser.id.in_({e.actor_id for e in entries if e.actor_id}))
            )
        }
        props = dict(
            self.session.execute(
                select(Property.id, Property.name).where(
                    Property.id.in_({e.property_id for e in entries if e.property_id})
                )
            ).all()
        )
        # One query per kind of record on the page.
        ids_by_type: dict[str, set[uuid.UUID]] = defaultdict(set)
        for e in entries:
            if e.entity_id and e.entity_type in MODELS:
                ids_by_type[e.entity_type].add(e.entity_id)
        found: dict[tuple[str, uuid.UUID], Any] = {}
        for entity_type, ids in ids_by_type.items():
            model = MODELS[entity_type]
            for record in self.session.scalars(select(model).where(model.id.in_(ids))):  # type: ignore[attr-defined]
                found[(entity_type, record.id)] = record
        # Current building/floor/space of each record, then their names in three queries.
        places: dict[tuple[str, uuid.UUID], tuple[Any, Any, Any]] = {}
        for (entity_type, record_id), record in found.items():
            model = MODELS[entity_type]
            space_id = record_id if model is Space else getattr(record, "space_id", None)
            floor_id = record_id if model is Floor else getattr(record, "floor_id", None)
            building_id = record_id if model is Building else getattr(record, "building_id", None)
            if model is not Property and (space_id or floor_id or building_id):
                places[(entity_type, record_id)] = (building_id, floor_id, space_id)
        names: dict[str, dict[uuid.UUID, str]] = {}
        for key, model, index in (("b", Building, 0), ("f", Floor, 1), ("s", Space, 2)):
            ids = {p[index] for p in places.values() if p[index]}
            names[key] = (
                dict(
                    self.session.execute(
                        select(model.id, model.name).where(model.id.in_(ids))
                    ).all()
                )
                if ids
                else {}
            )
        # A space's floor and building, when the record itself names only the space.
        missing = {p[2] for p in places.values() if p[2] and not p[1]}
        space_parent: dict[uuid.UUID, tuple[Any, Any]] = {}
        if missing:
            for sid, bid, fid in self.session.execute(
                select(Space.id, Space.building_id, Space.floor_id).where(Space.id.in_(missing))
            ):
                space_parent[sid] = (bid, fid)
            extra_b = {b for b, _ in space_parent.values() if b and b not in names["b"]}
            extra_f = {f for _, f in space_parent.values() if f and f not in names["f"]}
            if extra_b:
                names["b"].update(
                    self.session.execute(
                        select(Building.id, Building.name).where(Building.id.in_(extra_b))
                    ).all()
                )
            if extra_f:
                names["f"].update(
                    self.session.execute(
                        select(Floor.id, Floor.name).where(Floor.id.in_(extra_f))
                    ).all()
                )

        items = []
        for e in entries:
            key = (e.entity_type, e.entity_id) if e.entity_id else None
            record = found.get(key) if key else None
            if e.entity_id is None or e.entity_type not in MODELS:
                state = "UNKNOWN"
            elif record is None:
                state = "DELETED"
            elif getattr(record, "archived_at", None) is not None:
                state = "ARCHIVED"
            else:
                state = "EXISTS"
            name = next(
                (
                    getattr(record, f)
                    for f in NAME_FIELDS
                    if record is not None and getattr(record, f, None)
                ),
                None,
            )
            context = None
            if key and key in places:
                bid, fid, sid = places[key]
                if sid and not fid and sid in space_parent:
                    bid, fid = bid or space_parent[sid][0], space_parent[sid][1]
                context = CurrentContext(
                    building_id=bid,
                    building_name=names["b"].get(bid) if bid else None,
                    floor_id=fid,
                    floor_name=names["f"].get(fid) if fid else None,
                    space_id=sid,
                    space_name=names["s"].get(sid) if sid else None,
                )
            actor = actors.get(e.actor_id) if e.actor_id else None
            items.append(
                ActivityItem(
                    id=e.id,
                    created_at=e.created_at,
                    action=e.action,
                    action_label=action_label(e.action),
                    entity_type=e.entity_type,
                    entity_label=entity_label(e.entity_type),
                    summary=f"{entity_label(e.entity_type)}: {action_label(e.action).lower()}",
                    entity_id=e.entity_id,
                    message=e.message,
                    actor=ActivityActor(
                        id=actor.id, name=actor.full_name, email=actor.email, role=actor.role
                    )
                    if actor
                    else None,
                    property_id=e.property_id,
                    property_name=props.get(e.property_id) if e.property_id else None,
                    record=ActivityRecord(state=state, name=name),
                    current_context=context,
                    changes=recorded_changes(e.entity_type, e.action, e.message),
                )
            )
        return items

    def options(self) -> ActivityOptions:
        """Filter choices: only values that occur in stored entries, and actors and
        properties that still exist."""
        types = sorted(self.session.scalars(select(ActivityLog.entity_type).distinct()))
        actions = sorted(self.session.scalars(select(ActivityLog.action).distinct()))
        actors = self.session.execute(
            select(AppUser.id, AppUser.full_name, AppUser.email)
            .where(
                AppUser.id.in_(
                    select(ActivityLog.actor_id).where(ActivityLog.actor_id.is_not(None))
                )
            )
            .order_by(AppUser.email)
        ).all()
        props = self.session.execute(
            select(Property.id, Property.name)
            .where(
                Property.id.in_(
                    select(ActivityLog.property_id).where(ActivityLog.property_id.is_not(None))
                )
            )
            .order_by(Property.name)
        ).all()
        return ActivityOptions(
            entity_types=[ActivityOption(value=t, label=entity_label(t)) for t in types],
            actions=[ActivityOption(value=a, label=action_label(a)) for a in actions],
            actors=[
                ActivityOption(
                    value=str(i),
                    label=(f"{n} ({e})" if n and e else n or e or "User without an email"),
                )
                for i, n, e in actors
            ],
            properties=[ActivityOption(value=str(i), label=n) for i, n in props],
        )
