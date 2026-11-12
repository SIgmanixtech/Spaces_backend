"""Long-term rental requests from the client app (owner decision, 12 Nov 2026).

A customer asking for a space monthly (where the matrix says contract), quarterly,
half-yearly or yearly does not get a booking or a contract: the request becomes a CRM lead
(source CLIENT_APP), linked to the customer's tenant, with the space as its interest. The
property team then works it through the normal pipeline: proposal, Space Selected, contract
(the 409 gate before Space Selected stays), agreement signed by both parties, approval.

Nothing is held: the request reserves no space. It is refused if the space is already taken
(confirmed bookings and contracts) for the requested period. The price shown is an estimate:
the space's monthly price on the start day times the months; the deposit as configured.
No tax, advance or discount rule is applied.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.crm import Lead, LeadInterest, LeadNote
from app.models.enums import BillingPeriod, LeadSource
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.schemas.client_rentals import RentalEstimate, RentalRequestRead
from app.services.base import commit, flush
from app.services.catalogue import CatalogueService
from app.services.crm import CrmService
from app.services.lead_progress import LABEL, pipeline_stages
from app.services.occupancy import business_today
from app.services.pricing import PricingService
from app.services.rental_policy import TERM_MONTHS, Duration, can_request_contract

# A lead past these stages has its contract; another request for the space is fine then.
DONE = ("CONTRACT_COMPLETED", "TENANT_ACTIVATED")


def add_months(day: date, months: int) -> date:
    import calendar

    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class CustomerRentalService:
    def __init__(self, session: Session, user: AppUser, tenant: Tenant) -> None:
        self.session = session
        self.user = user
        self.tenant = tenant

    def _space(self, space_id: uuid.UUID) -> tuple[Space, Property]:
        return CatalogueService(self.session).get_space(space_id)

    def estimate(
        self, space_id: uuid.UUID, duration: Duration, start_date: date, count: int = 1
    ) -> RentalEstimate:
        space, prop = self._space(space_id)
        ok, reason = can_request_contract(space.space_type, duration)
        if not ok:
            raise ConflictError(reason or "Not offered.")
        if start_date < business_today():
            raise BadRequestError("The rental cannot start in the past.")
        if not 1 <= count <= 24:
            raise BadRequestError("Choose between 1 and 24 periods.")
        months = TERM_MONTHS[duration] * count
        if months > 36:
            raise BadRequestError("A request can cover at most 36 months.")
        end_date = add_months(start_date, months) - timedelta(days=1)
        try:
            _, estimate = PricingService(self.session).estimate(
                space.id, BillingPeriod.MONTHLY, on=start_date
            )
        except (NotFoundError, BadRequestError):
            raise ConflictError(
                f"{space.name} has no monthly price yet, so a long-term rental cannot be "
                "estimated. Contact the property team."
            ) from None
        monthly = Decimal(str(estimate.recurring_total))
        zone = ZoneInfo(get_settings().business_timezone)
        start_at = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(UTC)
        end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(
            UTC
        )
        state = CatalogueService(self.session).space_availability(space.id, start_at, end_at)
        return RentalEstimate(
            space_id=space.id,
            space_name=space.name,
            property_name=prop.name,
            duration=duration.value,
            months=months,
            start_date=start_date,
            end_date=end_date,
            currency=estimate.currency,
            monthly_rent=monthly,
            rent_total=monthly * months,
            deposit=Decimal(str(estimate.deposit)) if estimate.deposit is not None else None,
            available=state.available,
            availability_message=state.message,
        )

    def request(
        self,
        space_id: uuid.UUID,
        duration: Duration,
        start_date: date,
        count: int,
        notes: str | None,
    ) -> RentalRequestRead:
        est = self.estimate(space_id, duration, start_date, count)
        if not est.available:
            raise ConflictError(est.availability_message or "The space is taken for that period.")
        space, prop = self._space(space_id)
        for lead in self._mine():
            interest = self._interest(lead.id)
            if interest is not None and interest.space_id == space.id:
                stage = pipeline_stages(self.session, [lead])[lead.id]
                if stage not in DONE:
                    raise ConflictError(
                        f"You already asked for {space.name} ({lead.reference}). The property "
                        "team will contact you."
                    )
        crm = CrmService(self.session, self.user)
        lead = crm.insert(
            {
                "source": LeadSource.CLIENT_APP,
                "company_name": self.tenant.company_name,
                "contact_name": self.tenant.name,
                "contact_email": self.user.email or self.tenant.email,
                "contact_phone": self.tenant.phone,
                "property_id": prop.id,
                "space_type": space.space_type,
                "capacity": space.capacity,
                "desired_start_date": start_date,
                "currency": est.currency,
                "requested_term_months": est.months,
                "tenant_id": self.tenant.id,
                "tenant_linked_at": datetime.now(UTC),
            },
            how=" from the client app",
        )
        self.session.add(
            LeadInterest(
                lead_id=lead.id,
                property_id=prop.id,
                space_id=space.id,
                note=f"{est.duration.replace('_', '-').title()} rental, {est.months} month(s), "
                f"from {start_date}"[:500],
                created_by=self.user.id,
            )
        )
        deposit = f"; deposit {est.currency} {est.deposit}" if est.deposit is not None else ""
        body = (
            f"Long-term rental request from the client app: {space.name} at {prop.name}, "
            f"{est.duration.replace('_', '-').lower()} for {est.months} month(s), "
            f"{est.start_date} to {est.end_date}. Estimate at the monthly price: "
            f"{est.currency} {est.monthly_rent}/month, {est.currency} {est.rent_total} in total"
            f"{deposit}. Not a quote; nothing is held."
        )
        if notes and notes.strip():
            body += f"\nCustomer's note: {notes.strip()[:2000]}"
        self.session.add(LeadNote(lead_id=lead.id, body=body, created_by=self.user.id))
        flush(self.session)
        commit(self.session)
        return self._read(lead)

    def list(self) -> list[RentalRequestRead]:
        return [self._read(lead) for lead in self._mine()]

    def _mine(self) -> list[Lead]:
        return list(
            self.session.scalars(
                select(Lead)
                .where(Lead.tenant_id == self.tenant.id, Lead.source == LeadSource.CLIENT_APP)
                .order_by(Lead.created_at.desc())
            )
        )

    def _interest(self, lead_id: uuid.UUID) -> LeadInterest | None:
        return self.session.scalar(
            select(LeadInterest)
            .where(LeadInterest.lead_id == lead_id, LeadInterest.space_id.is_not(None))
            .order_by(LeadInterest.created_at)
            .limit(1)
        )

    def _read(self, lead: Lead) -> RentalRequestRead:
        interest = self._interest(lead.id)
        space = (
            self.session.get(Space, interest.space_id) if interest and interest.space_id else None
        )
        prop = self.session.get(Property, lead.property_id) if lead.property_id else None
        stage = pipeline_stages(self.session, [lead])[lead.id]
        return RentalRequestRead(
            id=lead.id,
            reference=lead.reference,
            property_name=prop.name if prop else None,
            space_name=space.name if space else None,
            space_type=space.space_type if space else None,
            months=lead.requested_term_months,
            start_date=lead.desired_start_date,
            stage=stage,
            stage_label=LABEL.get(stage, stage),
            created_at=lead.created_at,
        )
