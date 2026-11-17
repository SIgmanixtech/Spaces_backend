"""Contracts: a tenant holding a property, or one space and everything inside it, by date.

A contract is a ``tenant_occupancies`` row with a lifecycle:

    PENDING --activate--> ACTIVE --terminate--> TERMINATED
       |                    |  \\--extend--> ACTIVE (new end date)
       +------cancel--------+--> CANCELLED

PENDING and CANCELLED hold nothing, except a PENDING contract a customer requested from
the client app: it holds its space while it waits for the property team, so no one else
can take it, and rejecting (cancelling) it frees the space at once. ACTIVE and TERMINATED
hold their dates
(TERMINATED up to the termination date). An ACTIVE contract whose last day has
passed reads as EXPIRED and simply stops holding when its period ends; nothing
has to run for that. Records are never deleted to release a space.

A contract on a space holds it through the allocation engine, so whole-floor and
portion contracts block everything inside them and are blocked by anything held
inside them, in the database. A whole-property contract (no space) holds every
space of the property: it takes the property row exclusively, so it and the
engine's writers (which share that row) run one at a time and see each other.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.activity import ActivityLog
from app.models.agreement import ContractDocument
from app.models.allocation import Allocation
from app.models.booking import Booking
from app.models.enums import (
    CATEGORIES_NEEDING_END_DATE,
    CATEGORY_REQUIRED_FIELDS,
    HOLDING_CONTRACT_STATUSES,
    AllocationSource,
    BillingPeriod,
    BookingStatus,
    ContractCategory,
    ContractStatus,
    DocumentStatus,
    PropertyStatus,
    SpaceOperationalStatus,
    SpaceType,
)
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.repositories.base import paginate_rows
from app.schemas.common import ListQuery
from app.schemas.contract import (
    ContractAvailability,
    ContractRead,
    ContractScope,
    ContractScopeSpace,
)
from app.schemas.dashboard import ActivityEntry
from app.services.allocations import (
    AllocationConflict,
    AllocationEngine,
    Conflict,
    contract_period,
)
from app.services.base import commit, flush, log_activity, require
from app.services.client_access import completed_contract_condition, completed_contract_ids
from app.services.occupancy import business_today

ENTITY_TYPES = ("contracts", "tenant_occupancies")
# Owner decision D12.5 (2 Nov 2026): a pending contract holds its space for 7 days by
# default; the property team may set another day.
HOLD_DAYS = 7
# Category and its details (D8, D10), set on every row of a several-seat contract together.
CATEGORY_FIELDS = (
    "category",
    "main_contract_id",
    "expected_headcount",
    "occupants",
    "government_department",
    "government_order_reference",
    "vehicle_registration",
    "lock_in_period",
    "notice_period",
    "payment_due_day",
    "tds_percent",
)
# Terms an addendum changes once the agreement is signed (LC-3).
TERM_FIELDS = (
    "monthly_rent",
    "security_deposit",
    "terms",
    "lock_in_period",
    "notice_period",
    "payment_due_day",
    "tds_percent",
    "expected_headcount",
    "occupants",
    "government_department",
    "government_order_reference",
    "vehicle_registration",
)
FIELD_LABEL = {
    "expected_headcount": "expected headcount",
    "occupants": "number of occupants",
    "government_department": "government department",
    "government_order_reference": "government order reference",
    "vehicle_registration": "vehicle registration",
}


def effective_status(contract: TenantOccupancy, today: date) -> str:
    if (
        contract.status == ContractStatus.ACTIVE
        and contract.end_date is not None
        and contract.end_date < today
    ):
        return "EXPIRED"
    return contract.status


def _dates_overlap(start_a: date, end_a: date | None, start_b: date, end_b: date | None) -> bool:
    return (end_b is None or start_a <= end_b) and (end_a is None or start_b <= end_a)


class ContractService:
    def __init__(self, session: Session, actor: AppUser | None = None) -> None:
        self.session = session
        self.actor = actor
        self.engine = AllocationEngine(session)

    # --- reads -----------------------------------------------------------------

    def list(
        self,
        query: ListQuery,
        *,
        property_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        space_id: uuid.UUID | None = None,
        status: str | None = None,
        completed: bool | None = None,
    ) -> tuple[list[ContractRead], int]:
        statement = select(TenantOccupancy)
        if completed is not None:
            # BR-025, the same rule as client access: approved (ACTIVE) and signed.
            done = completed_contract_condition()
            statement = statement.where(done if completed else ~done)
        for column, value in (
            (TenantOccupancy.property_id, property_id),
            (TenantOccupancy.tenant_id, tenant_id),
            (TenantOccupancy.space_id, space_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        today = business_today()
        if status == "EXPIRED":
            statement = statement.where(
                TenantOccupancy.status == ContractStatus.ACTIVE, TenantOccupancy.end_date < today
            )
        elif status == ContractStatus.ACTIVE:
            statement = statement.where(
                TenantOccupancy.status == ContractStatus.ACTIVE,
                or_(TenantOccupancy.end_date.is_(None), TenantOccupancy.end_date >= today),
            )
        elif status is not None:
            statement = statement.where(TenantOccupancy.status == status)
        if query.search:
            statement = statement.where(TenantOccupancy.reference.ilike(f"%{query.search}%"))
        statement = statement.order_by(TenantOccupancy.start_date.desc(), TenantOccupancy.id)
        rows, total = paginate_rows(self.session, statement, query)
        return self.to_read([row[0] for row in rows]), total

    def get(self, contract_id: uuid.UUID) -> ContractRead:
        return self.to_read([self._get(contract_id)])[0]

    def scope(self, contract_id: uuid.UUID) -> ContractScope:
        contract = self._get(contract_id)
        spaces: Sequence[Space] = []
        if contract.space_id is not None:
            tree = select(Space.id).where(Space.id == contract.space_id).cte(recursive=True)
            tree = tree.union_all(select(Space.id).where(Space.parent_space_id == tree.c.id))
            spaces = self.session.scalars(
                select(Space)
                .where(Space.id.in_(select(tree.c.id)))
                .order_by(Space.depth, Space.name)
            ).all()
        return ContractScope(
            contract=self.to_read([contract])[0],
            spaces=[
                ContractScopeSpace(
                    id=s.id,
                    name=s.name,
                    code=s.code,
                    space_type=s.space_type,
                    depth=s.depth,
                    parent_space_id=s.parent_space_id,
                    archived=s.archived_at is not None,
                )
                for s in spaces
            ],
        )

    def history(self, contract_id: uuid.UUID) -> list[ActivityEntry]:
        contract = self._get(contract_id)
        rows = self.session.execute(
            select(ActivityLog, Property.name)
            .outerjoin(Property, Property.id == ActivityLog.property_id)
            .where(ActivityLog.entity_id == contract.id, ActivityLog.entity_type.in_(ENTITY_TYPES))
            .order_by(ActivityLog.created_at, ActivityLog.id)
        ).all()
        return [
            ActivityEntry(
                id=entry.id,
                action=entry.action,
                entity_type=entry.entity_type,
                message=entry.message,
                property_id=entry.property_id,
                property_name=name,
                created_at=entry.created_at,
            )
            for entry, name in rows
        ]

    def availability(
        self,
        *,
        property_id: uuid.UUID,
        space_id: uuid.UUID | None,
        start_date: date,
        end_date: date | None,
        tenant_id: uuid.UUID | None = None,
        exclude_contract_id: uuid.UUID | None = None,
    ) -> ContractAvailability:
        """From committed records. Activating checks again under the engine's locks."""
        if end_date is not None and end_date < start_date:
            raise BadRequestError("end_date cannot be before start_date.")
        prop = require(self.session, Property, property_id, "Property")
        space = self._space(prop.id, space_id) if space_id else None
        try:
            self._check_free(prop, space, start_date, end_date, tenant_id, exclude_contract_id)
        except ConflictError as exc:
            return ContractAvailability(
                available=False,
                message=exc.detail,
                conflicts=exc.conflicts or [],  # type: ignore[arg-type]
            )
        return ContractAvailability(available=True, message="Available.", conflicts=[])

    # --- writes ----------------------------------------------------------------

    def create(self, values: dict[str, Any]) -> ContractRead:
        activate = bool(values.pop("activate", False))
        tenant = require(self.session, Tenant, values["tenant_id"], "Tenant")
        prop = require(self.session, Property, values["property_id"], "Property")
        if values.get("space_id") is not None:
            self._space(prop.id, values["space_id"])
        contract = TenantOccupancy(
            **values,
            status=ContractStatus.PENDING,
            created_by=self.actor.id if self.actor else None,
        )
        self._check_category(contract)
        self.session.add(contract)
        flush(self.session)
        self.session.refresh(contract, ["reference"])  # assigned by the database
        self._log("created", contract, f'for tenant "{tenant.name}" at "{prop.name}"')
        if activate:
            self._activate(contract)
        commit(self.session)
        return self.get(contract.id)

    def create_from_proposal(self, proposal_id: uuid.UUID) -> ContractRead:
        """A PENDING contract from an accepted proposal (Phase 4B), for the tenant its lead
        is linked to. Copies the property, spaces, dates, rent, deposit, terms and other
        charges, and links the proposal. Several spaces become one contract group (one
        row per space); the proposal's rent and deposit are totals, so they go on the
        first row and the other rows carry no rent of their own. Holds nothing: no
        booking, allocation or activation, and no tenant is created."""
        from app.models.crm import Lead, Proposal, ProposalSpace
        from app.models.enums import LeadStage, ProposalStatus

        proposal = self.session.scalar(
            select(Proposal).where(Proposal.id == proposal_id).with_for_update()
        )
        if proposal is None:
            raise NotFoundError("Proposal not found.")
        if proposal.status != ProposalStatus.ACCEPTED:
            raise ConflictError(
                f"Only an accepted proposal becomes a contract; {proposal.reference} is "
                f"{proposal.status.lower()}."
            )
        lead = self.session.get(Lead, proposal.lead_id)
        # Owner decision 2 Nov 2026 (lead -> contract, question 3): a contract is made only
        # once the lead has reached Space Selected. Proposals themselves may come earlier.
        if lead is not None and lead.stage != LeadStage.SPACE_SELECTED:
            raise ConflictError(
                f"Lead {lead.reference} has not reached Space Selected yet. Move the lead to "
                "Space Selected first; then the contract can be created."
            )
        if lead is None or lead.tenant_id is None:
            raise ConflictError(
                "Link the lead to a tenant first (CRM, lead page); no tenant is created here."
            )
        existing = self.session.scalar(
            select(TenantOccupancy.reference).where(
                TenantOccupancy.proposal_id == proposal.id,
                TenantOccupancy.status != ContractStatus.CANCELLED,
            )
        )
        if existing:
            raise ConflictError(
                f"Contract {existing} was already made from {proposal.reference}. Cancel it "
                "first to make another."
            )
        tenant = require(self.session, Tenant, lead.tenant_id, "Tenant")
        prop = require(self.session, Property, proposal.property_id, "Property")
        space_ids = list(
            self.session.scalars(
                select(ProposalSpace.space_id)
                .where(ProposalSpace.proposal_id == proposal.id)
                .order_by(ProposalSpace.created_at, ProposalSpace.id)
            )
        )
        for space_id in space_ids:
            self._space(prop.id, space_id)
        group_id = uuid.uuid4() if len(space_ids) > 1 else None
        fields = {"other_charges": proposal.other_charges} if proposal.other_charges else {}
        made: list[TenantOccupancy] = []
        for i, space_id in enumerate(space_ids or [None]):
            first = i == 0
            contract = TenantOccupancy(
                tenant_id=tenant.id,
                property_id=prop.id,
                space_id=space_id,
                group_id=group_id,
                proposal_id=proposal.id,
                start_date=proposal.start_date,
                end_date=proposal.end_date,
                monthly_rent=proposal.monthly_rent
                if first
                else (0 if proposal.monthly_rent is not None else None),
                security_deposit=proposal.security_deposit if first else None,
                currency=proposal.currency,
                terms=proposal.commercial_terms,
                contract_fields=fields,
                notes=f"Made from proposal {proposal.reference}"
                + (f" (one of {len(space_ids)} spaces)." if group_id else "."),
                status=ContractStatus.PENDING,
                created_by=self.actor.id if self.actor else None,
            )
            self._check_order(contract)
            self.session.add(contract)
            flush(self.session)
            self.session.refresh(contract, ["reference"])
            self._log(
                "created",
                contract,
                f'for tenant "{tenant.name}" at "{prop.name}" from proposal {proposal.reference}',
            )
            made.append(contract)
        from app.services.lead_progress import record_progress

        record_progress(
            self.session,
            self.actor,
            [lead.id],
            f"contract {made[0].reference} created from proposal {proposal.reference}",
        )
        commit(self.session)
        return self.get(made[0].id)

    def request(self, values: dict[str, Any]) -> TenantOccupancy:
        """A customer's contract request: PENDING, but holding its space at once. The
        property team approves it (activate) or rejects it (cancel, which frees the space)."""
        prop = require(self.session, Property, values["property_id"], "Property")
        self._space(prop.id, values["space_id"])
        contract = TenantOccupancy(
            **values,
            status=ContractStatus.PENDING,
            created_by=self.actor.id if self.actor else None,
        )
        # D12.6: a customer's request holds its space like any pending contract, 7 days.
        if contract.hold_until is None:
            contract.hold_until = business_today() + timedelta(days=HOLD_DAYS)
        self.session.add(contract)
        flush(self.session)
        self.session.refresh(contract, ["reference"])
        self._hold(contract)
        self._log("requested", contract, f"holding the space until {contract.hold_until}")
        commit(self.session)
        return contract

    def update(self, contract_id: uuid.UUID, values: dict[str, Any]) -> ContractRead:
        contract = self._get(contract_id)
        dates = {k: values.pop(k) for k in ("start_date", "end_date") if k in values}
        if dates and contract.status != ContractStatus.PENDING:
            raise ConflictError(
                "Dates of an activated contract change only by extending or terminating it."
            )
        # LC-3/LC-4: once the agreement is signed, its terms change only by an addendum, so
        # a signed agreement is never silently replaced. Notes and category stay editable.
        changed_terms = sorted(
            k for k in TERM_FIELDS if k in values and values[k] != getattr(contract, k)
        )
        if (
            changed_terms
            and self._agreement_statuses({contract.id}).get(contract.id) == DocumentStatus.SIGNED
        ):
            raise ConflictError(
                "The agreement is signed. Change "
                + ", ".join(k.replace("_", " ") for k in changed_terms)
                + " with an addendum, which applies once both parties sign it."
            )
        shared = {k: values.pop(k) for k in CATEGORY_FIELDS if k in values}
        for field, value in {**values, **dates}.items():
            setattr(contract, field, value)
        members = self._group(contract, contract.status) if shared else [contract]
        for member in members:
            for field, value in shared.items():
                setattr(member, field, value)
            self._check_category(member)
        self._check_order(contract)
        if dates and self._is_holding(contract):
            # A requested contract already holds its space: move the hold with the dates.
            self._hold(contract)
        self._log("updated", contract)
        commit(self.session)
        return self.get(contract_id)

    def activate(self, contract_id: uuid.UUID) -> ContractRead:
        """Approving one seat of a group approves every pending seat of it, together."""
        contract = self._lock(contract_id)
        if contract.status != ContractStatus.PENDING:
            raise ConflictError(f"A {contract.status.lower()} contract cannot be activated.")
        members = self._group(contract, ContractStatus.PENDING)
        for member in members:
            self._check_ready(member)
        for member in members:
            self._activate(member)
            member.hold_extension_requested_until = None
            member.hold_extension_reason = None
        self._lead_progress(members, f"contract {contract.reference} approved")
        commit(self.session)
        return self.get(contract_id)

    def _lead_progress(self, contracts: Sequence[TenantOccupancy], cause: str) -> None:
        """Record the automatic lead stage (owner decision L-2) of the leads these
        contracts came from, in the same transaction."""
        from app.services.lead_progress import leads_of_contracts, record_progress

        leads = leads_of_contracts(self.session, [c.id for c in contracts])
        if leads:
            record_progress(self.session, self.actor, leads, cause)

    def cancel(
        self, contract_id: uuid.UUID, reason: str | None, *, include_parking: bool = True
    ) -> ContractRead:
        contract = self._lock(contract_id)
        if contract.status == ContractStatus.CANCELLED:
            raise ConflictError("The contract is already cancelled.")
        if contract.status == ContractStatus.TERMINATED:
            raise ConflictError("A terminated contract cannot be cancelled.")
        if contract.status == ContractStatus.ACTIVE and contract.start_date <= business_today():
            raise ConflictError(
                "The contract has taken effect. Terminate it instead, so its history is kept."
            )
        members = self._group(contract, contract.status)
        for member in members:
            self._cancel(member, reason)
        if include_parking:
            # LC-5: linked parking ends with its main contract unless the admin says no.
            note = f"Main contract {contract.reference} was cancelled."
            for parking in self._linked_parking(members, (ContractStatus.PENDING,)):
                self._cancel(parking, note)
            for parking in self._linked_parking(members, (ContractStatus.ACTIVE,)):
                if parking.start_date > business_today():
                    self._cancel(parking, note)
        self._lead_progress(members, f"contract {contract.reference} cancelled")
        commit(self.session)
        return self.get(contract_id)

    def terminate(
        self,
        contract_id: uuid.UUID,
        termination_date: date,
        reason: str | None,
        *,
        include_parking: bool = True,
    ) -> ContractRead:
        contract = self._lock(contract_id)
        if contract.status != ContractStatus.ACTIVE:
            raise ConflictError(f"A {contract.status.lower()} contract cannot be terminated.")
        if termination_date < contract.start_date:
            raise BadRequestError(
                "The termination date is before the contract starts. Cancel it instead."
            )
        if contract.end_date is not None and termination_date > contract.end_date:
            raise BadRequestError("The termination date is after the contract ends.")
        self._terminate(contract, termination_date, reason)
        if include_parking:
            # LC-5: linked parking ends with its main contract unless the admin says no.
            note = f"Main contract {contract.reference} was terminated."
            for parking in self._linked_parking([contract], (ContractStatus.ACTIVE,)):
                if parking.start_date > termination_date:
                    self._cancel(parking, note)
                elif parking.end_date is None or parking.end_date > termination_date:
                    self._terminate(parking, termination_date, note)
            for parking in self._linked_parking([contract], (ContractStatus.PENDING,)):
                self._cancel(parking, note)
        commit(self.session)
        return self.get(contract_id)

    def extend(
        self, contract_id: uuid.UUID, end_date: date | None, *, include_parking: bool = True
    ) -> ContractRead:
        """Date-only change (an extension). Changes to terms go through an addendum."""
        contract = self._lock(contract_id)
        if contract.status != ContractStatus.ACTIVE:
            raise ConflictError(f"A {contract.status.lower()} contract cannot be extended.")
        if end_date is not None and end_date < contract.start_date:
            raise BadRequestError("end_date cannot be before start_date.")
        if end_date is None and contract.category in CATEGORIES_NEEDING_END_DATE:
            raise BadRequestError(
                f"A {str(contract.category).lower()} contract needs an end date; it cannot be "
                "made ongoing."
            )
        before = contract.end_date
        parking = (
            [
                p
                for p in self._linked_parking([contract], (ContractStatus.ACTIVE,))
                if p.end_date == before
            ]
            if include_parking
            else []
        )
        for row in [contract, *parking]:
            row.end_date = end_date
            self._hold(row)
            self._log(
                "extended",
                row,
                f"from {before.isoformat() if before else 'ongoing'} to "
                f"{end_date.isoformat() if end_date else 'ongoing'}"
                + (f" with main contract {contract.reference}" if row is not contract else ""),
            )
        commit(self.session)
        return self.get(contract_id)

    # --- holds (owner decisions D12.4-D12.6) ------------------------------------------

    def hold(self, contract_id: uuid.UUID, until: date | None, reason: str | None) -> ContractRead:
        """Hold a pending contract's space until ``until`` (default 7 days from today), or
        move an existing hold. Checks the space is free first; refused with 409 if not."""
        contract = self._lock(contract_id)
        if contract.status != ContractStatus.PENDING:
            raise ConflictError("Only a pending contract is held; an approved one holds anyway.")
        if contract.space_id is None:
            raise ConflictError(
                "A contract for the whole property cannot be held while pending; approve it."
            )
        today = business_today()
        until = until or today + timedelta(days=HOLD_DAYS)
        if until < today:
            raise BadRequestError("The hold cannot end in the past.")
        members = self._group(contract, ContractStatus.PENDING)
        before = contract.hold_until if self._is_holding(contract) else None
        for member in members:
            member.hold_until = until
            member.hold_extension_requested_until = None
            member.hold_extension_reason = None
            self._hold(member)
            self._log(
                "hold_changed" if before else "held",
                member,
                (f"hold moved from {before} to {until}" if before else f"holding until {until}")
                + (f": {reason}" if reason else ""),
            )
        commit(self.session)
        return self.get(contract_id)

    def release_hold(self, contract_id: uuid.UUID, reason: str | None) -> ContractRead:
        contract = self._lock(contract_id)
        if contract.status != ContractStatus.PENDING or not self._is_holding(contract):
            raise ConflictError("The contract does not hold its space.")
        for member in self._group(contract, ContractStatus.PENDING):
            allocation = self.engine.for_contract(member.id)
            if allocation is not None:
                self.engine.release(allocation, "HOLD_RELEASED")
            self._log("hold_released", member, f": {reason}" if reason else "")
        commit(self.session)
        return self.get(contract_id)

    def decline_hold_extension(self, contract_id: uuid.UUID, reason: str | None) -> ContractRead:
        contract = self._lock(contract_id)
        if contract.hold_extension_requested_until is None:
            raise ConflictError("No extension of the hold was asked for.")
        for member in self._group(contract, contract.status):
            asked = member.hold_extension_requested_until
            member.hold_extension_requested_until = None
            member.hold_extension_reason = None
            self._log(
                "hold_extension_declined",
                member,
                f"(asked until {asked})" + (f": {reason}" if reason else ""),
            )
        commit(self.session)
        return self.get(contract_id)

    def ask_hold_extension(self, contract: TenantOccupancy, until: date, reason: str) -> None:
        """The customer asks for more time; the property team approves (hold) or declines."""
        if contract.status != ContractStatus.PENDING or not self._is_holding(contract):
            raise ConflictError("Only a request that still holds its space can be extended.")
        if contract.hold_until is not None and until <= contract.hold_until:
            raise BadRequestError("Ask for a day after the current hold ends.")
        reason = " ".join((reason or "").split())
        if not reason:
            raise BadRequestError("Say why more time is needed.")
        for member in self._group(contract, ContractStatus.PENDING):
            member.hold_extension_requested_until = until
            member.hold_extension_reason = reason[:2000]
            self._log("hold_extension_requested", member, f"until {until}: {reason[:200]}")
        commit(self.session)

    # --- renewal and ending soon (LC-2, LC-7) -----------------------------------------

    def renew(self, contract_id: uuid.UUID, values: dict[str, Any]) -> ContractRead:
        """A new PENDING contract from the day after this one ends, linked to it (LC-2).
        Copies tenant, space, category and its details, type and terms; rent, deposit,
        end date, terms and notes may be given new values. Linked parking ending on the same
        day is renewed with it (LC-6) unless ``include_parking`` is false. Nothing is held
        until the renewal is approved."""
        include_parking = bool(values.pop("include_parking", True))
        contract = self._lock(contract_id)
        if contract.status != ContractStatus.ACTIVE:
            raise ConflictError(f"A {contract.status.lower()} contract cannot be renewed.")
        if contract.end_date is None:
            raise ConflictError("An ongoing contract has no end to renew from; extend it instead.")
        made = self._renew_rows(self._group(contract, ContractStatus.ACTIVE), values)
        if include_parking:
            for parking in self._linked_parking([contract], (ContractStatus.ACTIVE,)):
                if parking.end_date == contract.end_date:
                    keep = {"end_date": made[0].end_date}
                    for row in self._renew_rows([parking], keep):
                        row.main_contract_id = made[0].id
        commit(self.session)
        return self.get(made[0].id)

    def _renew_rows(
        self, rows: list[TenantOccupancy], values: dict[str, Any]
    ) -> list[TenantOccupancy]:
        first = rows[0]
        existing = self.session.scalar(
            select(TenantOccupancy.reference).where(
                TenantOccupancy.renews_contract_id == first.id,
                TenantOccupancy.status != ContractStatus.CANCELLED,
            )
        )
        if existing:
            raise ConflictError(f"{first.reference} is already renewed by {existing}.")
        assert first.end_date is not None
        start = first.end_date + timedelta(days=1)
        if "end_date" in values:
            end = values["end_date"]
        else:
            end = start + (first.end_date - first.start_date)
        if end is not None and end < start:
            raise BadRequestError(f"The renewal starts on {start}; its end cannot be earlier.")
        if end is None and first.category in CATEGORIES_NEEDING_END_DATE:
            raise BadRequestError(f"A {str(first.category).lower()} contract needs an end date.")
        group_id = uuid.uuid4() if len(rows) > 1 else None
        made = []
        for i, old in enumerate(rows):
            new = TenantOccupancy(
                tenant_id=old.tenant_id,
                property_id=old.property_id,
                space_id=old.space_id,
                group_id=group_id,
                renews_contract_id=old.id,
                proposal_id=old.proposal_id,
                start_date=start,
                end_date=end,
                monthly_rent=values.get("monthly_rent", old.monthly_rent)
                if i == 0
                else old.monthly_rent,
                security_deposit=values.get("security_deposit", old.security_deposit)
                if i == 0
                else old.security_deposit,
                currency=old.currency,
                terms=values.get("terms", old.terms),
                contract_type=old.contract_type,
                customer_details=old.customer_details,
                contract_fields=old.contract_fields,
                notes=values.get("notes") or f"Renewal of {old.reference}.",
                status=ContractStatus.PENDING,
                created_by=self.actor.id if self.actor else None,
                **{f: getattr(old, f) for f in CATEGORY_FIELDS},
            )
            self.session.add(new)
            flush(self.session)
            self.session.refresh(new, ["reference"])
            self._log("created", new, f"as the renewal of {old.reference}, from {start}")
            self._log("renewed", old, f"by {new.reference}, from {start}")
            made.append(new)
        return made

    def ending_soon(self, days: int = 60) -> list[ContractRead]:
        """Approved contracts whose last day is within ``days`` (LC-7: 60 in the app),
        soonest first, each with the renewal already made, if any."""
        today = business_today()
        rows = self.session.scalars(
            select(TenantOccupancy)
            .where(
                TenantOccupancy.status == ContractStatus.ACTIVE,
                TenantOccupancy.end_date >= today,
                TenantOccupancy.end_date <= today + timedelta(days=days),
            )
            .order_by(TenantOccupancy.end_date, TenantOccupancy.reference)
        ).all()
        return self.to_read(rows)

    # --- booking to contract (D12.1-D12.3) ---------------------------------------------

    def convert_booking(self, booking_id: uuid.UUID, values: dict[str, Any]) -> ContractRead:
        """Turn a monthly booking into a PENDING contract for its space, prefilled from the
        booking and its pricing, holding the space for 7 days (D12.4). Hourly, daily and
        weekly bookings stay bookings. The booking is kept and reads as CONVERTED, linked
        to the contract; it stops holding its time and the contract holds instead, all in
        one transaction."""
        from app.services.bookings import BookingService

        booking = self.session.scalar(
            select(Booking).where(Booking.id == booking_id).with_for_update()
        )
        if booking is None:
            raise NotFoundError("Booking not found.")
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            raise ConflictError(
                f"A {booking.status.lower()} booking cannot be turned into a contract."
            )
        # D12.1-D12.2: monthly or longer. A customer booking states its billing period; an
        # admin booking has none, so its length decides (at least one calendar month).
        first_day = booking.start_at.astimezone(_zone()).date()
        last_moment = booking.end_at.astimezone(_zone())
        long_enough = last_moment.date() >= _one_month_after(first_day)
        if booking.billing_period not in (None, BillingPeriod.MONTHLY) or (
            booking.billing_period is None and not long_enough
        ):
            period = (booking.billing_period or "shorter than a month").lower().replace("_", " ")
            raise ConflictError(
                f"Only a booking of a month or longer becomes a contract; this one is {period}. "
                "Hourly, daily and weekly bookings stay bookings."
            )
        tenant_id = values.get("tenant_id") or booking.tenant_id
        if tenant_id is None:
            raise BadRequestError(
                "The booking has no tenant. Choose the tenant the contract is for."
            )
        tenant = require(self.session, Tenant, tenant_id, "Tenant")
        start = values.get("start_date") or max(
            business_today(), booking.start_at.astimezone(_zone()).date()
        )
        if "end_date" in values:
            end = values["end_date"]
        else:
            end = max(booking.end_at.astimezone(_zone()).date() - timedelta(days=1), start)
        rent = values.get("monthly_rent")
        deposit = values.get("security_deposit")
        if rent is None or deposit is None:
            suggestion = self.suggest_pricing(booking.space_id, start)
            rent = suggestion["monthly_rent"] if rent is None else rent
            deposit = suggestion["security_deposit"] if deposit is None else deposit
        BookingService(self.session, self.actor).cancel(
            booking.id, "Turned into a contract by the property team.", action="converted"
        )
        contract = TenantOccupancy(
            tenant_id=tenant.id,
            property_id=booking.property_id,
            space_id=booking.space_id,
            booking_id=booking.id,
            start_date=start,
            end_date=end,
            monthly_rent=rent,
            security_deposit=deposit,
            currency=booking.quoted_currency or "INR",
            notes=values.get("notes") or f"Made from booking {booking.reference}.",
            status=ContractStatus.PENDING,
            hold_until=business_today() + timedelta(days=HOLD_DAYS),
            created_by=self.actor.id if self.actor else None,
            **{f: values[f] for f in CATEGORY_FIELDS if values.get(f) is not None},
        )
        self._check_order(contract)
        self._check_category(contract)
        self.session.add(contract)
        flush(self.session)
        self.session.refresh(contract, ["reference"])
        self._hold(contract)
        self._log(
            "created",
            contract,
            f"from booking {booking.reference}, holding the space until {contract.hold_until}",
        )
        commit(self.session)
        return self.get(contract.id)

    def suggest_pricing(self, space_id: uuid.UUID | None, on: date) -> dict[str, Any]:
        """D10.3-D10.4: rent and deposit suggested from the space's monthly pricing. The
        admin may change them; a deposit of zero is allowed."""
        from app.services.pricing import PricingService

        out: dict[str, Any] = {"monthly_rent": None, "security_deposit": None}
        if space_id is None:
            return out
        try:
            _, estimate = PricingService(self.session).estimate(
                space_id, BillingPeriod.MONTHLY, on=on
            )
        except (NotFoundError, BadRequestError):
            return out
        out["monthly_rent"] = Decimal(str(estimate.recurring_total))
        if estimate.deposit is not None:
            out["security_deposit"] = Decimal(str(estimate.deposit))
        return out

    # --- used by the occupancy endpoints, which predate contracts ---------------------

    def create_active(self, tenant_id: uuid.UUID, values: dict[str, Any]) -> TenantOccupancy:
        read = self.create({**values, "tenant_id": tenant_id, "activate": True})
        return self._get(read.id)

    def change_dates(self, contract: TenantOccupancy, values: dict[str, Any]) -> None:
        """PATCH /occupancies/{id}: any dates, re-checked against the hierarchy."""
        for field, value in values.items():
            setattr(contract, field, value)
        self._check_order(contract)
        if contract.status in HOLDING_CONTRACT_STATUSES and {"start_date", "end_date"} & set(
            values
        ):
            self._hold(contract)
        self._log("updated", contract)
        commit(self.session)

    def withdraw(self, contract: TenantOccupancy) -> None:
        """DELETE /occupancies/{id}: the record is kept as CANCELLED, the space released."""
        self._lock(contract.id)
        self._cancel(contract, "Removed through the occupancy endpoint")
        commit(self.session)

    # --- internals ---------------------------------------------------------------------

    def _get(self, contract_id: uuid.UUID) -> TenantOccupancy:
        contract = self.session.get(TenantOccupancy, contract_id)
        if contract is None:
            raise NotFoundError("Contract not found.")
        return contract

    def _group(self, contract: TenantOccupancy, status: str) -> list[TenantOccupancy]:
        """The contract and, for a several-seat contract, its other rows in the same
        status, locked. Rows in another state (already ended, say) are left alone."""
        if contract.group_id is None:
            return [contract]
        others = self.session.scalars(
            select(TenantOccupancy)
            .where(
                TenantOccupancy.group_id == contract.group_id,
                TenantOccupancy.id != contract.id,
                TenantOccupancy.status == status,
            )
            .order_by(TenantOccupancy.reference)
            .with_for_update()
        ).all()
        return [contract, *others]

    def _lock(self, contract_id: uuid.UUID) -> TenantOccupancy:
        # Two simultaneous lifecycle changes of one contract must not both act on it.
        contract = self.session.scalar(
            select(TenantOccupancy).where(TenantOccupancy.id == contract_id).with_for_update()
        )
        if contract is None:
            raise NotFoundError("Contract not found.")
        return contract

    def _space(self, property_id: uuid.UUID, space_id: uuid.UUID) -> Space:
        space = require(self.session, Space, space_id, "Space")
        if space.property_id != property_id:
            raise BadRequestError("The space does not belong to this property.")
        return space

    def _is_holding(self, contract: TenantOccupancy) -> bool:
        allocation = self.engine.for_contract(contract.id)
        return allocation is not None and allocation.is_active

    def _linked_parking(
        self, mains: Sequence[TenantOccupancy], statuses: tuple[str, ...]
    ) -> list[TenantOccupancy]:
        return list(
            self.session.scalars(
                select(TenantOccupancy)
                .where(
                    TenantOccupancy.main_contract_id.in_([m.id for m in mains]),
                    TenantOccupancy.status.in_(statuses),
                )
                .order_by(TenantOccupancy.reference)
                .with_for_update()
            )
        )

    def _terminate(
        self, contract: TenantOccupancy, termination_date: date, reason: str | None
    ) -> None:
        if contract.original_end_date is None:
            contract.original_end_date = contract.end_date
        contract.end_date = termination_date
        contract.status = ContractStatus.TERMINATED
        contract.terminated_at = datetime.now(UTC)
        contract.termination_reason = reason
        self._sync_allocation(contract)
        self._log(
            "terminated",
            contract,
            f"with effect after {termination_date.isoformat()}" + (f": {reason}" if reason else ""),
        )

    def _check_category(self, contract: TenantOccupancy) -> None:
        """D8.4-D8.5: only a Parking contract links to a main contract; the main contract
        is the same tenant's, not itself Parking and not cancelled."""
        if contract.main_contract_id is None:
            return
        if contract.category != ContractCategory.PARKING:
            raise BadRequestError("Only a Parking contract can be linked to a main contract.")
        main = self.session.get(TenantOccupancy, contract.main_contract_id)
        if main is None:
            raise BadRequestError("The main contract does not exist.")
        if main.id == contract.id:
            raise BadRequestError("A contract cannot be its own main contract.")
        if main.tenant_id != contract.tenant_id:
            raise BadRequestError("The main contract belongs to another tenant.")
        if main.category == ContractCategory.PARKING:
            raise BadRequestError("A Parking contract cannot be the main contract.")
        if main.status == ContractStatus.CANCELLED:
            raise BadRequestError(f"The main contract {main.reference} is cancelled.")

    def _missing(self, contract: TenantOccupancy) -> list[str]:
        """What a categorised contract still needs before approval (D10.1-D10.6). Contracts
        without a category (made before categories, D8.6) are approved as before."""
        if contract.category is None:
            return []
        missing = [
            FIELD_LABEL[f]
            for f in CATEGORY_REQUIRED_FIELDS.get(contract.category, ())
            if getattr(contract, f) in (None, "")
        ]
        if contract.end_date is None and contract.category in CATEGORIES_NEEDING_END_DATE:
            missing.append("end date")
        # D10.3: rent required. A several-seat contract carries the rent on its first row.
        if contract.monthly_rent is None and contract.group_id is None:
            missing.append("monthly rent")
        return missing

    def _check_ready(self, contract: TenantOccupancy) -> None:
        missing = self._missing(contract)
        if missing:
            raise ConflictError(
                f"Contract {contract.reference} ({str(contract.category).lower()}) needs "
                + ", ".join(missing)
                + " before it can be approved."
            )

    @staticmethod
    def _check_order(contract: TenantOccupancy) -> None:
        if contract.end_date is not None and contract.end_date < contract.start_date:
            raise BadRequestError("end_date cannot be before start_date.")

    def _activate(self, contract: TenantOccupancy) -> None:
        contract.status = ContractStatus.ACTIVE
        contract.activated_at = datetime.now(UTC)
        self._hold(contract)
        self._log("activated", contract)

    def _cancel(self, contract: TenantOccupancy, reason: str | None) -> None:
        contract.status = ContractStatus.CANCELLED
        contract.cancelled_at = datetime.now(UTC)
        contract.cancellation_reason = reason
        allocation = self.engine.for_contract(contract.id)
        if allocation is not None:
            self.engine.release(allocation, "CONTRACT_CANCELLED")
        self._log("cancelled", contract, f": {reason}" if reason else "")

    def _hold(self, contract: TenantOccupancy) -> None:
        """Checks the contract's dates are free, then (re)writes its allocation."""
        prop = require(self.session, Property, contract.property_id, "Property")
        space = self._space(prop.id, contract.space_id) if contract.space_id else None
        if space is None:
            self.engine.own_property(prop.id)
        else:
            self.engine.share_property(prop.id)
        self._check_free(
            prop, space, contract.start_date, contract.end_date, contract.tenant_id, contract.id
        )
        flush(self.session)
        self._sync_allocation(contract)

    def _sync_allocation(self, contract: TenantOccupancy) -> None:
        if contract.space_id is None:
            return
        period = contract_period(contract.start_date, contract.end_date)
        allocation = self.engine.for_contract(contract.id)
        if allocation is None:
            self.engine.allocate(
                source=AllocationSource.CONTRACT,
                record_id=contract.id,
                property_id=contract.property_id,
                space_id=contract.space_id,
                period=period,
            )
        else:
            self.engine.change_period(allocation, period)

    def _check_free(
        self,
        prop: Property,
        space: Space | None,
        start_date: date,
        end_date: date | None,
        tenant_id: uuid.UUID | None,
        exclude_contract_id: uuid.UUID | None,
    ) -> None:
        if prop.archived_at is not None or prop.status != PropertyStatus.ACTIVE:
            raise ConflictError("The property is not open for contracts.")
        if space is not None:
            if space.archived_at is not None:
                raise ConflictError("The space is archived.")
            if space.operational_status != SpaceOperationalStatus.ACTIVE:
                raise ConflictError("The space is not open for contracts.")
            # A whole floor is contracted through its root, whatever its rentable flag.
            if space.space_type != SpaceType.FLOOR and (
                not space.is_rentable or space.space_type == SpaceType.NON_RENTABLE
            ):
                raise ConflictError("The space is not rentable.")

        whole = self._whole_property_contracts(prop.id, start_date, end_date, exclude_contract_id)
        others = [c for c in whole if c.tenant_id != tenant_id]
        if others:
            raise ConflictError(
                f"The whole property is held by contract {others[0].reference} for part of "
                "that time."
            )

        own = self.engine.for_contract(exclude_contract_id) if exclude_contract_id else None
        period = contract_period(start_date, end_date)
        if space is not None:
            found = self.engine.conflicts(space.id, period, exclude=[own.id] if own else [])
            if found:
                raise AllocationConflict(found)
            return

        # The whole property: nothing of anyone else's may be held on any of its spaces.
        held = self.session.scalars(
            select(Allocation).where(
                Allocation.property_id == prop.id,
                Allocation.is_active,
                Allocation.period.op("&&")(func.tstzrange(period.lower, period.upper, "[)")),
            )
        ).all()
        foreign = [a for a in held if self._holder(a) != tenant_id]
        if foreign:
            found = self.engine.describe(foreign, uuid.uuid4(), set())
            raise AllocationConflict(
                found,
                f"{found[0].space_name} is held by {found[0].reference} for part of that time, "
                "so the whole property cannot be contracted.",
            )

    def _holder(self, allocation: Allocation) -> uuid.UUID | None:
        if allocation.contract_id:
            return self.session.scalar(
                select(TenantOccupancy.tenant_id).where(
                    TenantOccupancy.id == allocation.contract_id
                )
            )
        return self.session.scalar(
            select(Booking.tenant_id).where(Booking.id == allocation.booking_id)
        )

    def _whole_property_contracts(
        self,
        property_id: uuid.UUID,
        start_date: date,
        end_date: date | None,
        exclude_contract_id: uuid.UUID | None,
    ) -> list[TenantOccupancy]:
        statement = select(TenantOccupancy).where(
            TenantOccupancy.property_id == property_id,
            TenantOccupancy.space_id.is_(None),
            TenantOccupancy.status.in_(HOLDING_CONTRACT_STATUSES),
        )
        if exclude_contract_id is not None:
            statement = statement.where(TenantOccupancy.id != exclude_contract_id)
        return [
            c
            for c in self.session.scalars(statement)
            if _dates_overlap(start_date, end_date, c.start_date, c.end_date)
        ]

    def _log(self, action: str, contract: TenantOccupancy, suffix: str = "") -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type="contracts",
            entity_id=contract.id,
            property_id=contract.property_id,
            at=datetime.now(UTC),
            message=f"Contract {contract.reference} was {action}"
            + (f" {suffix}" if suffix and not suffix.startswith(":") else suffix)
            + ".",
        )

    def to_read(self, contracts: Sequence[TenantOccupancy]) -> list[ContractRead]:
        if not contracts:
            return []
        tenants = dict(
            self.session.execute(
                select(Tenant.id, Tenant.name).where(
                    Tenant.id.in_({c.tenant_id for c in contracts})
                )
            ).all()
        )
        props = dict(
            self.session.execute(
                select(Property.id, Property.name).where(
                    Property.id.in_({c.property_id for c in contracts})
                )
            ).all()
        )
        spaces = {
            row.id: row
            for row in self.session.execute(
                select(Space.id, Space.name, Space.space_type).where(
                    Space.id.in_({c.space_id for c in contracts if c.space_id})
                )
            )
        }
        groups = group_details(self.session, {c.group_id for c in contracts if c.group_id})
        ids = {c.id for c in contracts}
        agreements = self._agreement_statuses(ids)
        completed = completed_contract_ids(self.session, ids)
        today = business_today()
        link_ids = {x for c in contracts for x in (c.main_contract_id, c.renews_contract_id) if x}
        links = (
            dict(
                self.session.execute(
                    select(TenantOccupancy.id, TenantOccupancy.reference).where(
                        TenantOccupancy.id.in_(link_ids)
                    )
                ).all()
            )
            if link_ids
            else {}
        )
        renewals = dict(
            self.session.execute(
                select(TenantOccupancy.renews_contract_id, TenantOccupancy.reference).where(
                    TenantOccupancy.renews_contract_id.in_(ids),
                    TenantOccupancy.status != ContractStatus.CANCELLED,
                )
            ).all()
        )
        # A customer's pending request holds its space too; read the holds, not the status.
        held = {
            c.id for c in contracts if c.status == ContractStatus.PENDING and self._is_holding(c)
        }
        out = []
        for c in contracts:
            space = spaces.get(c.space_id) if c.space_id else None
            status = effective_status(c, today)
            out.append(
                ContractRead(
                    id=c.id,
                    reference=c.reference,
                    tenant_id=c.tenant_id,
                    tenant_name=tenants.get(c.tenant_id, ""),
                    property_id=c.property_id,
                    property_name=props.get(c.property_id, ""),
                    space_id=c.space_id,
                    space_name=space.name if space else None,
                    space_type=space.space_type if space else None,
                    start_date=c.start_date,
                    end_date=c.end_date,
                    original_end_date=c.original_end_date,
                    status=status,  # type: ignore[arg-type]
                    holds_inventory=c.status in HOLDING_CONTRACT_STATUSES or c.id in held,
                    monthly_rent=c.monthly_rent,
                    security_deposit=c.security_deposit,
                    currency=c.currency,
                    terms=c.terms,
                    contract_type=c.contract_type,
                    notes=c.notes,
                    activated_at=c.activated_at,
                    cancelled_at=c.cancelled_at,
                    cancellation_reason=c.cancellation_reason,
                    terminated_at=c.terminated_at,
                    termination_reason=c.termination_reason,
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                    group_id=c.group_id,
                    group_reference=groups[c.group_id][0] if c.group_id in groups else None,
                    group_spaces=groups[c.group_id][1] if c.group_id in groups else [],
                    agreement_status=agreements.get(c.id),
                    proposal_id=c.proposal_id,
                    completes_contract=c.id in completed,
                    category=c.category,
                    main_contract_id=c.main_contract_id,
                    main_contract_reference=links.get(c.main_contract_id),
                    renews_contract_id=c.renews_contract_id,
                    renews_contract_reference=links.get(c.renews_contract_id),
                    renewed_by_reference=renewals.get(c.id),
                    hold_until=c.hold_until if c.status == ContractStatus.PENDING else None,
                    hold_extension_requested_until=c.hold_extension_requested_until,
                    hold_extension_reason=c.hold_extension_reason,
                    expected_headcount=c.expected_headcount,
                    occupants=c.occupants,
                    government_department=c.government_department,
                    government_order_reference=c.government_order_reference,
                    vehicle_registration=c.vehicle_registration,
                    lock_in_period=c.lock_in_period,
                    notice_period=c.notice_period,
                    payment_due_day=c.payment_due_day,
                    tds_percent=c.tds_percent,
                    booking_id=c.booking_id,
                    missing_for_approval=self._missing(c) if c.status == "PENDING" else [],
                )
            )
        return out

    def _agreement_statuses(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, DocumentStatus]:
        """Each contract's agreement status, read-only: SIGNED if any document is signed,
        otherwise its newest document that is not superseded."""
        out: dict[uuid.UUID, DocumentStatus] = {}
        rows = self.session.execute(
            select(ContractDocument.contract_id, ContractDocument.status)
            .where(
                ContractDocument.contract_id.in_(ids),
                ContractDocument.status != DocumentStatus.SUPERSEDED,
            )
            .order_by(ContractDocument.created_at, ContractDocument.id)
        )
        for contract_id, status in rows:
            if out.get(contract_id) != DocumentStatus.SIGNED:
                out[contract_id] = DocumentStatus(status)
        return out


def group_details(
    session: Session, group_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, list[str]]]:
    """For each several-seat contract: its first reference and every seat's name."""
    if not group_ids:
        return {}
    rows = session.execute(
        select(TenantOccupancy.group_id, TenantOccupancy.reference, Space.name)
        .join(Space, Space.id == TenantOccupancy.space_id)
        .where(TenantOccupancy.group_id.in_(group_ids))
        .order_by(TenantOccupancy.reference)
    ).all()
    out: dict[uuid.UUID, tuple[str, list[str]]] = {}
    for group_id, reference, name in rows:
        first, names = out.get(group_id, (reference, []))
        names.append(name)
        out[group_id] = (first, names)
    return out


def group_members(session: Session, contract: TenantOccupancy) -> list[TenantOccupancy]:
    """Every row of a several-seat contract, first reference first; or just the contract."""
    if contract.group_id is None:
        return [contract]
    return list(
        session.scalars(
            select(TenantOccupancy)
            .where(TenantOccupancy.group_id == contract.group_id)
            .order_by(TenantOccupancy.reference)
        )
    )


__all__ = ["Conflict", "ContractService", "effective_status", "group_details", "group_members"]


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().business_timezone)


def _one_month_after(day: date) -> date:
    """The same day next month, or that month's last day (31 Jan -> 28/29 Feb)."""
    import calendar

    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))
