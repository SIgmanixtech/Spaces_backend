"""The signed-in customer's own account, bookings and contracts.

Ownership always starts from the access token: the customer account (app_users,
role CUSTOMER) owns exactly one tenant record (tenants.user_id), and every
booking or contract shown or changed here must belong to that tenant. Ids in a
request are only ever looked up *within* that tenant's records; a record of
anyone else answers 404, the same as one that does not exist.
"""

from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import TokenVerifier
from app.models.activity import ActivityLog
from app.models.booking import Booking
from app.models.enums import (
    BillingPeriod,
    BookingChannel,
    BookingStatus,
    ContractStatus,
    SpaceType,
    UserRole,
)
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.schemas.client import (
    CustomerActivity,
    CustomerBooking,
    CustomerContract,
    CustomerDashboard,
    CustomerProfile,
    LeaseDue,
    OtpResult,
)
from app.schemas.common import ListQuery
from app.services.auth import (
    SupabaseSession,
    open_session,
)
from app.services.base import commit, one_transaction
from app.services.bookings import BookingService, converted_fields, converted_to
from app.services.catalogue import CatalogueService
from app.services.client_access import NOT_ACTIVATED
from app.services.contracts import ContractService, effective_status, group_members
from app.services.occupancy import business_today
from app.services.pricing import PricingService, periods_in

CANCELLABLE = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
TYPE_LABEL = {
    SpaceType.FLOOR: "Whole floor",
    SpaceType.PORTION: "Portion",
}


class CustomerService:
    def __init__(self, session: Session, user: AppUser) -> None:
        self.session = session
        self.user = user

    # --- account -----------------------------------------------------------------

    def tenant(self) -> Tenant | None:
        """The tenant this account belongs to. Never created here: tenants come from the
        property team (BR-005); client endpoints already require an activated tenant."""
        return self.session.scalar(select(Tenant).where(Tenant.user_id == self.user.id))

    def _own_tenant(self) -> Tenant:
        tenant = self.tenant()
        if tenant is None:
            raise ForbiddenError(NOT_ACTIVATED)
        return tenant

    def profile(self) -> CustomerProfile:
        tenant = self._own_tenant()
        return CustomerProfile(
            id=self.user.id,
            email=self.user.email or tenant.email,
            full_name=self.user.full_name or tenant.name,
            phone=tenant.phone,
            company_name=tenant.company_name,
        )

    def update_profile(self, values: dict[str, object]) -> CustomerProfile:
        tenant = self._own_tenant()
        if values.get("full_name"):
            self.user.full_name = str(values["full_name"])
            tenant.name = str(values["full_name"])
        for field in ("phone", "company_name"):
            if field in values:
                setattr(tenant, field, values[field] or None)
        commit(self.session)
        return self.profile()

    # --- bookings ----------------------------------------------------------------

    def bookings(
        self, query: ListQuery, status: BookingStatus | None = None
    ) -> tuple[list[CustomerBooking], int]:
        tenant = self.tenant()
        if tenant is None:
            return [], 0
        statement = select(Booking).where(Booking.tenant_id == tenant.id)
        if status is not None:
            statement = statement.where(Booking.status == status)
        total = self.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        rows = self.session.scalars(
            statement.order_by(Booking.start_at.desc(), Booking.id)
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        ).all()
        return self._read(rows), total

    def booking(self, booking_id: uuid.UUID) -> CustomerBooking:
        return self._read([self._own_booking(booking_id)])[0]

    def book(
        self,
        catalogue: CatalogueService,
        *,
        space_id: uuid.UUID,
        billing_period: BillingPeriod,
        start_at: datetime,
        end_at: datetime,
        notes: str | None,
        group_id: uuid.UUID | None = None,
    ) -> CustomerBooking:
        """Price and hold a space for the customer. The booking starts PENDING: an
        administrator confirms it, as with every other booking."""
        space, prop = catalogue.get_space(space_id)
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise BadRequestError("Give times with a time zone, e.g. 2026-11-02T09:00:00+05:30.")
        quantity = periods_in(billing_period, start_at, end_at)
        try:
            price, estimate = PricingService(self.session).estimate(
                space.id, billing_period, on=catalogue._business_day(start_at), quantity=quantity
            )
        except NotFoundError:
            raise ConflictError(
                f"{space.name} has no {billing_period.lower().replace('_', '-')} price for those "
                "dates, so it cannot be booked that way."
            ) from None
        tenant = self._own_tenant()
        values = {
            "property_id": prop.id,
            "space_id": space.id,
            "tenant_id": tenant.id,
            "customer_name": tenant.name,
            "customer_email": self.user.email,
            "customer_phone": tenant.phone,
            "start_at": start_at,
            "end_at": end_at,
            "notes": notes,
            "group_id": group_id,
            "status": BookingStatus.PENDING,
            "channel": BookingChannel.CUSTOMER,
            "billing_period": billing_period,
            "pricing_rule_id": price.rule.id,
            "quoted_total": estimate.recurring_total,
            "quoted_currency": estimate.currency,
            "quote": estimate.model_dump(mode="json"),
        }
        try:
            created = BookingService(self.session, self.user).create(values)
        except ConflictError:
            # The booking service names the holds in the way, which may be other
            # customers'. Say only what this space's state is.
            self.session.rollback()
            state = catalogue.space_availability(space.id, start_at, end_at)
            message = (
                state.message
                if not state.available
                else "The space was taken while you were booking. Choose another time."
            )
            if group_id is not None and space.name not in message:
                message = f"{space.name}: {message}"
            raise ConflictError(message) from None
        return self.booking(created.id)

    def book_group(
        self,
        catalogue: CatalogueService,
        *,
        space_ids: list[uuid.UUID],
        billing_period: BillingPeriod,
        start_at: datetime,
        end_at: datetime,
        notes: str | None,
    ) -> list[CustomerBooking]:
        """Several spaces for the same time, booked together: every one is priced and held,
        or nothing is booked. They share a group id, so a contract can be requested for
        all of them at once."""
        group_id = uuid.uuid4()
        made: list[uuid.UUID] = []
        try:
            with one_transaction(self.session):
                for space_id in space_ids:
                    booked = self.book(
                        catalogue,
                        space_id=space_id,
                        billing_period=billing_period,
                        start_at=start_at,
                        end_at=end_at,
                        notes=notes,
                        group_id=group_id,
                    )
                    made.append(booked.id)
            commit(self.session)
        except BaseException:
            self.session.rollback()
            raise
        rows = self.session.scalars(select(Booking).where(Booking.id.in_(made))).all()
        order = {booking_id: i for i, booking_id in enumerate(made)}
        return self._read(sorted(rows, key=lambda b: order[b.id]))

    def reschedule(
        self,
        catalogue: CatalogueService,
        booking_id: uuid.UUID,
        *,
        billing_period: BillingPeriod,
        start_at: datetime,
        end_at: datetime,
    ) -> CustomerBooking:
        """Move one of the customer's bookings, re-priced and re-checked on the server.
        It goes back to PENDING: the property team confirms the new time."""
        booking = self._own_booking(booking_id)
        if booking.status not in CANCELLABLE:
            raise ConflictError(f"A {booking.status.lower()} booking cannot be changed.")
        if booking.start_at <= datetime.now(UTC):
            raise ConflictError(
                "The booking has already started. Contact the property team to change it."
            )
        space, _ = catalogue.get_space(booking.space_id)
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise BadRequestError("Give times with a time zone, e.g. 2026-11-02T09:00:00+05:30.")
        values = self._quote(catalogue, space, billing_period, start_at, end_at)
        values.update({"start_at": start_at, "end_at": end_at, "status": BookingStatus.PENDING})
        try:
            BookingService(self.session, self.user).update(booking.id, values)
        except ConflictError:
            self.session.rollback()
            state = catalogue.space_availability(space.id, start_at, end_at)
            raise ConflictError(
                state.message
                if not state.available
                else "That time is no longer free. Choose another time."
            ) from None
        return self.booking(booking.id)

    def _quote(
        self,
        catalogue: CatalogueService,
        space: Space,
        billing_period: BillingPeriod,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, object]:
        quantity = periods_in(billing_period, start_at, end_at)
        try:
            price, estimate = PricingService(self.session).estimate(
                space.id, billing_period, on=catalogue._business_day(start_at), quantity=quantity
            )
        except NotFoundError:
            raise ConflictError(
                f"{space.name} has no {billing_period.lower().replace('_', '-')} price for those "
                "dates, so it cannot be booked that way."
            ) from None
        return {
            "billing_period": billing_period,
            "pricing_rule_id": price.rule.id,
            "quoted_total": estimate.recurring_total,
            "quoted_currency": estimate.currency,
            "quote": estimate.model_dump(mode="json"),
        }

    def cancel(self, booking_id: uuid.UUID, reason: str | None) -> CustomerBooking:
        booking = self._own_booking(booking_id)
        if booking.status not in CANCELLABLE:
            raise ConflictError(f"A {booking.status.lower()} booking cannot be cancelled.")
        if booking.start_at <= datetime.now(UTC):
            raise ConflictError(
                "The booking has already started. Contact the property team to change it."
            )
        BookingService(self.session, self.user).cancel(
            booking.id, (reason or "Cancelled by the customer.")[:500]
        )
        return self.booking(booking.id)

    def _own_booking(self, booking_id: uuid.UUID) -> Booking:
        tenant = self.tenant()
        booking = (
            self.session.scalar(
                select(Booking).where(Booking.id == booking_id, Booking.tenant_id == tenant.id)
            )
            if tenant
            else None
        )
        if booking is None:
            raise NotFoundError("Booking not found.")
        return booking

    def _read(self, bookings: list[Booking] | tuple[Booking, ...]) -> list[CustomerBooking]:
        if not bookings:
            return []
        spaces = {
            s.id: s
            for s in self.session.scalars(
                select(Space).where(Space.id.in_({b.space_id for b in bookings}))
            )
        }
        props = dict(
            self.session.execute(
                select(Property.id, Property.name).where(
                    Property.id.in_({b.property_id for b in bookings})
                )
            ).all()
        )
        now = datetime.now(UTC)
        contracts = converted_to(self.session, bookings)
        group_ids = {b.group_id for b in bookings if b.group_id}
        sizes = (
            dict(
                self.session.execute(
                    select(Booking.group_id, func.count())
                    .where(Booking.group_id.in_(group_ids))
                    .group_by(Booking.group_id)
                ).all()
            )
            if group_ids
            else {}
        )
        return [
            CustomerBooking(
                **{"status": BookingStatus(b.status), **converted_fields(b, contracts)},
                id=b.id,
                reference=b.reference,
                property_id=b.property_id,
                property_name=props.get(b.property_id, "Property"),
                space_id=b.space_id,
                space_name=spaces[b.space_id].name,
                space_type=SpaceType(spaces[b.space_id].space_type),
                start_at=b.start_at,
                end_at=b.end_at,
                billing_period=BillingPeriod(b.billing_period) if b.billing_period else None,
                quoted_total=float(b.quoted_total) if b.quoted_total is not None else None,
                quoted_currency=b.quoted_currency,
                quote=b.quote,
                notes=b.notes,
                cancelled_at=b.cancelled_at,
                cancellation_reason=b.cancellation_reason,
                can_cancel=b.status in CANCELLABLE and b.start_at > now,
                created_at=b.created_at,
                group_id=b.group_id,
                group_size=sizes.get(b.group_id, 1) if b.group_id else 1,
            )
            for b in bookings
        ]

    # --- contracts ---------------------------------------------------------------

    def dashboard(self, upcoming_limit: int = 5, activity_limit: int = 10) -> CustomerDashboard:
        tenant = self.tenant()
        if tenant is None:
            return CustomerDashboard(
                upcoming=[], reserved=0, booked=0, active_leases=[], monthly_rent={}, activity=[]
            )
        now = datetime.now(UTC)
        open_rows = self.session.scalars(
            select(Booking)
            .where(
                Booking.tenant_id == tenant.id,
                Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
                Booking.end_at > now,
            )
            .order_by(Booking.start_at, Booking.id)
        ).all()
        today = business_today()
        leases = []
        seen_groups: set[uuid.UUID] = set()
        rent: dict[str, float] = {}
        for row in self.session.scalars(
            select(TenantOccupancy)
            .where(TenantOccupancy.tenant_id == tenant.id)
            .order_by(TenantOccupancy.start_date, TenantOccupancy.id)
        ):
            if effective_status(row, today) != ContractStatus.ACTIVE or row.start_date > today:
                continue
            if row.group_id is not None:
                if row.group_id in seen_groups:
                    continue
                seen_groups.add(row.group_id)
            contract = self._contract(row)
            due = _next_monthly_date(row.start_date, today, row.end_date)
            leases.append(LeaseDue(contract=contract, next_due_date=due))
            if contract.monthly_rent is not None:
                rent[contract.currency] = rent.get(contract.currency, 0.0) + contract.monthly_rent
        return CustomerDashboard(
            upcoming=self._read(list(open_rows[:upcoming_limit])),
            reserved=sum(1 for b in open_rows if b.status == BookingStatus.PENDING),
            contract_requests=self.session.scalar(
                select(
                    func.count(
                        func.distinct(func.coalesce(TenantOccupancy.group_id, TenantOccupancy.id))
                    )
                ).where(
                    TenantOccupancy.tenant_id == tenant.id,
                    TenantOccupancy.status == ContractStatus.PENDING,
                )
            )
            or 0,
            booked=sum(1 for b in open_rows if b.status == BookingStatus.CONFIRMED),
            active_leases=leases,
            monthly_rent=rent,
            activity=self._activity(tenant, activity_limit),
        )

    def _activity(self, tenant: Tenant, limit: int) -> list[CustomerActivity]:
        """What happened to the customer's own bookings and leases. The text is written here
        from the action, never copied from the log, so internal notes are not shown."""
        bookings = dict(
            self.session.execute(
                select(Booking.id, Booking.reference).where(Booking.tenant_id == tenant.id)
            ).all()
        )
        contract_rows = self.session.execute(
            select(TenantOccupancy.id, TenantOccupancy.reference, TenantOccupancy.group_id).where(
                TenantOccupancy.tenant_id == tenant.id
            )
        ).all()
        first_of: dict[uuid.UUID, str] = {}
        for _, reference, group_id in contract_rows:
            if group_id and (group_id not in first_of or reference < first_of[group_id]):
                first_of[group_id] = reference
        contracts = {
            row_id: first_of[group_id] if group_id else reference
            for row_id, reference, group_id in contract_rows
        }
        ids = [*bookings, *contracts]
        if not ids:
            return []
        rows = self.session.scalars(
            select(ActivityLog)
            .where(
                ActivityLog.entity_id.in_(ids),
                ActivityLog.entity_type.in_(["bookings", "contracts", "tenant_occupancies"]),
                ActivityLog.action.in_(list(_ACTIONS)),
            )
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id)
            .limit(limit)
        ).all()
        found = []
        said: set[tuple[str, str]] = set()
        for row in rows:
            is_booking = row.entity_type == "bookings"
            reference = (bookings if is_booking else contracts).get(row.entity_id)
            if reference is None:
                continue
            if not is_booking:
                # The seats of one contract change together: say it once.
                if (row.action, reference) in said:
                    continue
                said.add((row.action, reference))
            by_team = row.actor_id != self.user.id
            noun = "Booking" if is_booking else "Lease"
            who = " by the property team" if by_team else ""
            found.append(
                CustomerActivity(
                    at=row.created_at,
                    kind="BOOKING" if is_booking else "CONTRACT",
                    reference=reference,
                    action=row.action,
                    message=f"{noun} {reference} {_ACTIONS[row.action]}{who}.",
                    by_property_team=by_team,
                    booking_id=row.entity_id if is_booking else None,
                )
            )
        return found

    def request_contract(
        self, booking_id: uuid.UUID, start_date: date, months: int, notes: str | None
    ) -> CustomerContract:
        """Turn one of the customer's reservations into a contract request for that space.
        The booking is replaced by the request, which holds the space for the contract dates
        until the property team approves or rejects it. Both happen together or not at all."""
        booking = self._own_booking(booking_id)
        if booking.status not in CANCELLABLE or booking.end_at <= datetime.now(UTC):
            raise ConflictError("Only a current reservation can be turned into a contract.")
        if start_date < business_today():
            raise BadRequestError("The contract cannot start in the past.")
        end_date = _add_months(start_date, months)
        rent = None
        try:
            _, estimate = PricingService(self.session).estimate(
                booking.space_id, BillingPeriod.MONTHLY, on=start_date
            )
            rent = estimate.recurring_total
        except (NotFoundError, BadRequestError):
            pass
        tenant = self.tenant()
        # Seats booked together become one contract request covering all of them.
        members = [booking]
        if booking.group_id is not None:
            members += self.session.scalars(
                select(Booking)
                .where(
                    Booking.group_id == booking.group_id,
                    Booking.id != booking.id,
                    Booking.tenant_id == booking.tenant_id,
                    Booking.status.in_(CANCELLABLE),
                    Booking.end_at > datetime.now(UTC),
                )
                .order_by(Booking.reference)
            ).all()
            members.sort(key=lambda b: b.reference)
        group_id = uuid.uuid4() if len(members) > 1 else None
        try:
            with one_transaction(self.session):
                made = []
                for member in members:
                    BookingService(self.session, self.user).cancel(
                        member.id,
                        "Turned into a contract request by the customer.",
                        action="converted",
                    )
                    made.append(
                        ContractService(self.session, self.user).request(
                            {
                                "tenant_id": tenant.id,
                                "property_id": member.property_id,
                                "space_id": member.space_id,
                                "booking_id": member.id,
                                "group_id": group_id,
                                "start_date": start_date,
                                "end_date": end_date,
                                "monthly_rent": rent
                                if member.id == booking.id
                                else self._monthly_rent(member.space_id, start_date),
                                "currency": "INR",
                                "notes": (
                                    f"Requested by the customer from booking {member.reference}"
                                    + (f" (one of {len(members)} seats)." if group_id else ".")
                                    + (f" Note: {notes}" if notes else "")
                                ),
                            }
                        )
                    )
                contract = made[0]
            commit(self.session)
        except BaseException:
            self.session.rollback()
            raise
        return self._contract(self.session.get(TenantOccupancy, contract.id))

    def _monthly_rent(self, space_id: uuid.UUID | None, on: date) -> Any:
        if space_id is None:
            return None
        try:
            _, estimate = PricingService(self.session).estimate(
                space_id, BillingPeriod.MONTHLY, on=on
            )
        except (NotFoundError, BadRequestError):
            return None
        return estimate.recurring_total

    def own_contract(self, contract_id: uuid.UUID) -> TenantOccupancy:
        """One of the signed-in customer's contracts; anyone else's reads as missing."""
        tenant = self.tenant()
        row = (
            self.session.scalar(
                select(TenantOccupancy).where(
                    TenantOccupancy.id == contract_id, TenantOccupancy.tenant_id == tenant.id
                )
            )
            if tenant
            else None
        )
        if row is None:
            raise NotFoundError("Contract not found.")
        return row

    def withdraw_contract(self, contract_id: uuid.UUID) -> CustomerContract:
        """Withdraw a contract request that the property team has not approved yet."""
        tenant = self.tenant()
        row = (
            self.session.scalar(
                select(TenantOccupancy).where(
                    TenantOccupancy.id == contract_id, TenantOccupancy.tenant_id == tenant.id
                )
            )
            if tenant
            else None
        )
        if row is None:
            raise NotFoundError("Contract not found.")
        if row.status != ContractStatus.PENDING:
            raise ConflictError("Only a request that is still waiting can be withdrawn.")
        ContractService(self.session, self.user).cancel(row.id, "Withdrawn by the customer.")
        return self._contract(self.session.get(TenantOccupancy, row.id))

    def contracts(self) -> list[CustomerContract]:
        tenant = self.tenant()
        if tenant is None:
            return []
        rows = self.session.scalars(
            select(TenantOccupancy)
            .where(TenantOccupancy.tenant_id == tenant.id)
            .order_by(TenantOccupancy.start_date.desc(), TenantOccupancy.id)
        ).all()
        # A several-seat contract is listed once, under its first reference.
        first: dict[uuid.UUID, TenantOccupancy] = {}
        for c in rows:
            if c.group_id and (
                c.group_id not in first or c.reference < first[c.group_id].reference
            ):
                first[c.group_id] = c
        return [self._contract(c) for c in rows if not c.group_id or first[c.group_id] is c]

    def contract(self, contract_id: uuid.UUID) -> CustomerContract:
        tenant = self.tenant()
        row = (
            self.session.scalar(
                select(TenantOccupancy).where(
                    TenantOccupancy.id == contract_id, TenantOccupancy.tenant_id == tenant.id
                )
            )
            if tenant
            else None
        )
        if row is None:
            raise NotFoundError("Contract not found.")
        return self._contract(row)

    def _contract(self, c: TenantOccupancy) -> CustomerContract:
        prop = self.session.get(Property, c.property_id)
        space = self.session.get(Space, c.space_id) if c.space_id else None
        if space is None:
            scope = "Whole property"
        else:
            label = TYPE_LABEL.get(SpaceType(space.space_type))
            scope = f"{label}: {space.name}" if label else space.name
        members = group_members(self.session, c)
        names: list[str] = []
        rent = c.monthly_rent
        if len(members) > 1:
            spaces_by_id = {
                s.id: s
                for s in self.session.scalars(
                    select(Space).where(Space.id.in_({m.space_id for m in members if m.space_id}))
                )
            }
            names = [spaces_by_id[m.space_id].name for m in members if m.space_id in spaces_by_id]
            scope = f"{len(names)} spaces: {', '.join(names)}"
            rents = [m.monthly_rent for m in members]
            rent = sum(rents) if all(r is not None for r in rents) else None  # type: ignore[assignment,misc]
            c = members[0]
        elif space is not None:
            names = [space.name]
        return CustomerContract(
            group_id=c.group_id,
            spaces=names,
            id=c.id,
            reference=c.reference,
            property_id=c.property_id,
            property_name=prop.name if prop else "Property",
            space_name=space.name if space else None,
            space_type=space.space_type if space else None,
            scope=scope,
            start_date=c.start_date,
            end_date=c.end_date,
            status=effective_status(c, business_today()),
            monthly_rent=float(rent) if rent is not None else None,
            security_deposit=float(c.security_deposit) if c.security_deposit is not None else None,
            currency=c.currency,
            terms=c.terms,
        )


# Actions a customer is told about, and how they read.
_ACTIONS = {
    "created": "was made",
    "booked": "was made",
    "confirmed": "was confirmed",
    "rescheduled": "was moved to a new time",
    "completed": "was completed",
    "cancelled": "was cancelled",
    "converted": "was turned into a contract request",
    "requested": "was requested and is waiting for the property team",
    "activated": "started",
    "extended": "was extended",
    "renewed": "was renewed",
    "terminated": "was ended",
}


def _next_monthly_date(start: date, today: date, end: date | None) -> date | None:
    """The next day-of-month matching the lease start, on or after today, within the lease.
    Short months use their last day (a lease from the 31st is due on 30 April)."""
    year, month = today.year, today.month
    for _ in range(2):
        day = min(start.day, monthrange(year, month)[1])
        candidate = date(year, month, day)
        if candidate >= today and candidate >= start:
            return candidate if end is None or candidate <= end else None
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return None


def _add_months(start: date, months: int) -> date:
    """The last day of a term of whole months: 1 Jan + 12 months ends 31 Dec."""
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return date.fromordinal(date(year, month, day).toordinal() - 1)


def normalize_phone(raw: str) -> str:
    """E.164 for a mobile number. Indian numbers may be given as 10 digits, with 0 or 91,
    or with +91; other countries need their + code. Raises BadRequestError otherwise."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    plus = raw.strip().startswith("+")
    if not plus:
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            digits = "91" + digits
    if digits.startswith("91") and len(digits) == 12:
        if digits[2] not in "6789":
            raise BadRequestError("Enter a valid Indian mobile number.")
        return "+" + digits
    if plus and 8 <= len(digits) <= 15 and not digits.startswith("0"):
        return "+" + digits
    raise BadRequestError("Enter a valid mobile number, e.g. 98765 43210 or +91 98765 43210.")


def _phone_claim(phone: str | None) -> str | None:
    return ("+" + phone.lstrip("+")) if phone else None


def finish_code_sign_in(
    session: Session, verifier: TokenVerifier, supabase_session: SupabaseSession
) -> OtpResult:
    """After the provider verified a code: sign in a customer whose tenant is activated.
    Nothing is created here (BR-005); anyone else is told how access works."""
    claims = verifier.verify(supabase_session.access_token)
    user = session.get(AppUser, claims.subject)
    if user is None:
        raise ForbiddenError(NOT_ACTIVATED)
    if user.role != UserRole.CUSTOMER:
        raise ForbiddenError(
            "This is the customer app. Administrators sign in to the SIGMANIX admin application."
        )
    return OtpResult(status="SIGNED_IN", session=open_session(session, verifier, supabase_session))
