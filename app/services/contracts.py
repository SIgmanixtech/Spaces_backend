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
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.activity import ActivityLog
from app.models.agreement import ContractDocument
from app.models.allocation import Allocation
from app.models.booking import Booking
from app.models.enums import (
    HOLDING_CONTRACT_STATUSES,
    AllocationSource,
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
        from app.models.enums import ProposalStatus

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
        self.session.add(contract)
        flush(self.session)
        self.session.refresh(contract, ["reference"])
        self._hold(contract)
        self._log("requested", contract)
        commit(self.session)
        return contract

    def update(self, contract_id: uuid.UUID, values: dict[str, Any]) -> ContractRead:
        contract = self._get(contract_id)
        dates = {k: values.pop(k) for k in ("start_date", "end_date") if k in values}
        if dates and contract.status != ContractStatus.PENDING:
            raise ConflictError(
                "Dates of an activated contract change only by extending or terminating it."
            )
        for field, value in {**values, **dates}.items():
            setattr(contract, field, value)
        self._check_order(contract)
        if dates and self.engine.for_contract(contract.id) is not None:
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
        for member in self._group(contract, ContractStatus.PENDING):
            self._activate(member)
        commit(self.session)
        return self.get(contract_id)

    def cancel(self, contract_id: uuid.UUID, reason: str | None) -> ContractRead:
        contract = self._lock(contract_id)
        if contract.status == ContractStatus.CANCELLED:
            raise ConflictError("The contract is already cancelled.")
        if contract.status == ContractStatus.TERMINATED:
            raise ConflictError("A terminated contract cannot be cancelled.")
        if contract.status == ContractStatus.ACTIVE and contract.start_date <= business_today():
            raise ConflictError(
                "The contract has taken effect. Terminate it instead, so its history is kept."
            )
        for member in self._group(contract, contract.status):
            self._cancel(member, reason)
        commit(self.session)
        return self.get(contract_id)

    def terminate(
        self, contract_id: uuid.UUID, termination_date: date, reason: str | None
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
        commit(self.session)
        return self.get(contract_id)

    def extend(self, contract_id: uuid.UUID, end_date: date | None) -> ContractRead:
        contract = self._lock(contract_id)
        if contract.status != ContractStatus.ACTIVE:
            raise ConflictError(f"A {contract.status.lower()} contract cannot be extended.")
        if end_date is not None and end_date < contract.start_date:
            raise BadRequestError("end_date cannot be before start_date.")
        before = contract.end_date
        contract.end_date = end_date
        self._hold(contract)
        self._log(
            "extended",
            contract,
            f"from {before.isoformat() if before else 'ongoing'} to "
            f"{end_date.isoformat() if end_date else 'ongoing'}",
        )
        commit(self.session)
        return self.get(contract_id)

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
        # A customer's pending request holds its space too; read the holds, not the status.
        held = {
            c.id
            for c in contracts
            if c.status == ContractStatus.PENDING and self.engine.for_contract(c.id) is not None
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
