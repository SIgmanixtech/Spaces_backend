"""Adding a tenant with the space they take (owner request and decisions of 14 Nov 2026).

One admin action, one transaction:

1. The tenant: a new one, or an existing one linked instead. A new tenant whose email or
   phone matches existing tenants is refused (409, the matches listed) until the admin
   confirms it is a different person or company. Nothing is merged or changed.
2. The space: an ACTIVE contract for the chosen space and dates, made through the
   contract service, so the allocation engine's conflict checks apply (no second engine).
   Its monthly rent and deposit come from the space's pricing rule.
3. The account: opened now (owner decision 14 Nov 2026: a tenant the property team adds
   with a space gets an account at once; elsewhere BR-025 still waits for a completed
   contract).
4. The initial invoice, a DRAFT (the admin reviews and issues it): one full month from
   the start date (no proration rule exists), a Space Fee line for the rent and a
   Maintenance Fee line for the maintenance, amounts from the pricing engine. Tax and
   additional charges are shown in the breakdown but not invoiced: the invoice module has
   no tax lines and only the three charge categories (decisions D5-D7 stay open). No
   price, no invoice: the tenant and contract are still saved, and the reason is returned.
5. The signature image, if given: stored privately, referenced on the tenant. It is only
   a signature asset and never marks anything as signed.

Retrying the same request returns what the first one made: the same tenant's ACTIVE
contract for that space and start date is reused, and its initial invoice is never made
twice. If anything fails, nothing is saved and an uploaded image is removed again.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from app.models.account import Account
from app.models.building import Building, Floor
from app.models.enums import (
    BillingPeriod,
    ChargeCategory,
    ContractStatus,
    InvoiceStatus,
    SpaceOperationalStatus,
    SpaceType,
    TenantStatus,
)
from app.models.invoice import Invoice
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.schemas.tenant_onboarding import (
    AssignmentQuote,
    InvoicePreview,
    InvoicePreviewLine,
    OnboardResult,
    PricingBreakdown,
    QuoteSpace,
)
from app.services.allocations import AllocationEngine, contract_period
from app.services.base import log_activity, one_transaction, require
from app.services.customer_rentals import add_months
from app.services.occupancy import business_today
from app.services.pricing import PricingService, is_priceable
from app.services.tenant_signature import SignatureUpload, delete_replaced, store_signature
from app.storage import ObjectStorage

INITIAL_NOTE = "Initial invoice for contract {reference}: first month from the start date."
NOT_INVOICED = (
    "Tax and additional charges are shown above but not invoiced automatically: the invoice "
    "has Space Fee and Maintenance Fee lines only. Add other lines on the draft if they apply."
)


class DuplicateTenantError(ConflictError):
    code = "duplicate_tenant"


def _money(value: float | Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


class TenantOnboardingService:
    def __init__(
        self,
        session: Session,
        actor: AppUser,
        storage_factory: Callable[[], ObjectStorage] | None = None,
    ) -> None:
        self.session = session
        self.actor = actor
        self._storage_factory = storage_factory

    # --- the preview ------------------------------------------------------------------

    def _spaces(self, values: dict[str, Any]) -> tuple[Property, list[Space]]:
        prop = require(self.session, Property, values["property_id"], "Property")
        if prop.archived_at is not None:
            raise BadRequestError("The property is archived.")
        spaces: list[Space] = []
        for space_id in values["space_ids"]:
            space = require(self.session, Space, space_id, "Space")
            if space.property_id != prop.id:
                raise BadRequestError(f'"{space.name}" does not belong to this property.')
            if space.archived_at is not None:
                raise BadRequestError(f'"{space.name}" is archived.')
            if space.space_type != SpaceType.FLOOR and not is_priceable(space):
                raise BadRequestError(f'"{space.name}" is not rentable, so it cannot be assigned.')
            spaces.append(space)
        return prop, spaces

    def quote(self, values: dict[str, Any]) -> AssignmentQuote:
        prop, spaces = self._spaces(values)
        first = spaces[0]
        floor = self.session.get(Floor, first.floor_id) if first.floor_id else None
        building = self.session.get(Building, floor.building_id) if floor else None
        start, end = values["start_date"], values.get("end_date")
        engine = AllocationEngine(self.session)
        rows: list[QuoteSpace] = []
        for space in spaces:
            found = [c.describe() for c in engine.conflicts(space.id, contract_period(start, end))]
            if space.operational_status != SpaceOperationalStatus.ACTIVE:
                found.insert(0, f'"{space.name}" is not open (under maintenance or closed).')
            rows.append(
                QuoteSpace(
                    space_id=space.id,
                    space_name=space.name,
                    space_type=SpaceType(space.space_type),
                    area_sqft=_money(space.area_sqft),
                    available=not found,
                    conflicts=found,
                )
            )
        conflicts = [
            (f"{r.space_name}: {c}" if len(rows) > 1 else c) for r in rows for c in r.conflicts
        ]

        pricing: PricingBreakdown | None = None
        missing: str | None = None
        try:
            prices = [
                PricingService(self.session).estimate(s.id, BillingPeriod.MONTHLY, on=start)
                for s in spaces
            ]
        except (NotFoundError, BadRequestError) as exc:
            missing = exc.detail
        else:
            taxes = {(p.rule.tax_treatment, p.rule.tax_rate) for p, _ in prices}
            if len(taxes) > 1:
                missing = "The chosen spaces are priced with different GST; choose them separately."
            else:
                price, first_estimate = prices[0]
                rule = price.rule

                def total(attr: str) -> Decimal:
                    return sum((Decimal(str(getattr(e, attr))) for _, e in prices), Decimal(0))

                deposits = [Decimal(str(e.deposit)) for _, e in prices if e.deposit is not None]
                recurring, tax = total("recurring_total"), total("tax")
                tds_rate = values.get("tds_percent")
                tds = (
                    ((recurring - tax) * Decimal(tds_rate) / 100).quantize(Decimal("0.01"))
                    if tds_rate
                    else Decimal(0)
                )
                areas = [s.area_sqft for s in spaces]
                pricing = PricingBreakdown(
                    quantity=len(spaces),
                    source=str(price.source),
                    rule_id=rule.id,
                    currency=first_estimate.currency,
                    rate_basis=rule.rate_basis,
                    rate=_money(rule.amount),  # type: ignore[arg-type]
                    area_sqft=_money(sum(areas)) if all(areas) else None,  # type: ignore[arg-type]
                    maintenance_rate=_money(rule.maintenance_amount),
                    maintenance_basis=rule.maintenance_basis,
                    rent=total("rent"),
                    maintenance=total("maintenance"),
                    additional_charges=total("additional_charges"),
                    tax_treatment=rule.tax_treatment,
                    tax_rate=_money(rule.tax_rate),
                    tax=tax,
                    recurring_total=recurring,
                    deposit=sum(deposits, Decimal(0)) if deposits else None,
                    tds_percent=_money(tds_rate),
                    tds_amount=tds,
                    net_payable=recurring - tds,
                )
        areas_all = [s.area_sqft for s in spaces]
        return AssignmentQuote(
            property_id=prop.id,
            property_name=prop.name,
            building_name=building.name if building else None,
            floor_name=floor.name if floor else None,
            space_id=first.id,
            space_name=first.name if len(spaces) == 1 else ", ".join(s.name for s in spaces),
            space_type=SpaceType(first.space_type),
            area_sqft=_money(sum(areas_all)) if all(areas_all) else None,  # type: ignore[arg-type]
            capacity=first.capacity,
            available=not conflicts,
            conflicts=conflicts,
            pricing=pricing,
            pricing_missing=missing,
            invoice=self._invoice_preview(spaces, start, pricing) if pricing else None,
            quantity=len(spaces),
            spaces=rows,
        )

    @staticmethod
    def _invoice_preview(
        spaces: list[Space], start: Any, pricing: PricingBreakdown
    ) -> InvoicePreview:
        period_end = add_months(start, 1) - timedelta(days=1)
        names = (
            spaces[0].name
            if len(spaces) == 1
            else f"{len(spaces)} spaces ({', '.join(s.name for s in spaces)})"
        )
        where = f"{names}, {start:%d %b %Y} to {period_end:%d %b %Y}"
        lines = [
            InvoicePreviewLine(
                category=ChargeCategory.SPACE_FEE, description=f"Rent: {where}", amount=pricing.rent
            )
        ]
        if pricing.maintenance > 0:
            lines.append(
                InvoicePreviewLine(
                    category=ChargeCategory.MAINTENANCE_FEE,
                    description=f"Maintenance: {where}",
                    amount=pricing.maintenance,
                )
            )
        return InvoicePreview(
            period_start=start,
            period_end=period_end,
            currency=pricing.currency,
            lines=lines,
            total=sum((line.amount for line in lines), Decimal(0)),
            note=NOT_INVOICED,
        )

    # --- the action -------------------------------------------------------------------

    def onboard(self, body: dict[str, Any], signature: SignatureUpload | None) -> OnboardResult:
        from app.services.contracts import ContractService
        from app.services.invoices import InvoiceService
        from app.services.tenants import TenantService

        assignment = body["assignment"]
        uploaded: tuple[ObjectStorage, str] | None = None
        try:
            with one_transaction(self.session):
                tenant, created = self._tenant(body)
                quote = self.quote(assignment)
                contract = self._existing_contract(tenant, assignment)
                if contract is None:
                    if not quote.available:
                        raise ConflictError(quote.conflicts[0])
                    contract = self._contracts(tenant, assignment, quote)
                self._open_account(tenant)
                invoice_id, skipped = self._initial_invoice(tenant, contract, quote)
                if signature is not None:
                    storage = self._storage()
                    path = store_signature(self.session, storage, self.actor, tenant, signature)
                    uploaded = (storage, path)
            self.session.commit()
        except BaseException:
            self.session.rollback()
            self.session.info.pop("delete_after_commit", None)
            if uploaded is not None:
                with contextlib.suppress(Exception):  # the original error matters more
                    uploaded[0].delete(uploaded[1])
            raise
        if uploaded is not None:
            delete_replaced(self.session, uploaded[0])

        service = TenantService(self.session, self.actor)
        return OnboardResult(
            tenant=service.to_read([self.session.get(Tenant, tenant.id)])[0],  # type: ignore[list-item]
            created_tenant=created,
            contract=ContractService(self.session, self.actor).get(contract.id),
            invoice=InvoiceService(self.session, self.actor).get(invoice_id)
            if invoice_id
            else None,
            invoice_skipped=skipped,
            signature_saved=signature is not None,
        )

    def _tenant(self, body: dict[str, Any]) -> tuple[Tenant, bool]:
        from app.services.crm import CrmService
        from app.services.tenants import TenantService

        if body.get("existing_tenant_id") is not None:
            tenant = self.session.scalar(
                select(Tenant).where(Tenant.id == body["existing_tenant_id"]).with_for_update()
            )
            if tenant is None:
                raise NotFoundError("Tenant not found.")
            if tenant.archived_at is not None or tenant.status != TenantStatus.ACTIVE:
                raise ConflictError(f'Tenant "{tenant.name}" is archived or inactive.')
            return tenant, False
        values = dict(body["tenant"])
        matches = [
            m
            for m in CrmService(self.session, self.actor).check_duplicates(
                contact_email=values.get("email"),
                contact_phone=values.get("phone"),
                company_name=None,
                exclude_tenant_id=None,
                include_leads=False,
            )
            if m.kind == "TENANT" and ({"email", "phone"} & set(m.matched_on))
        ]
        if matches and not body.get("confirm_not_duplicate"):
            raise DuplicateTenantError(
                "A tenant with the same email or phone already exists. Link the existing "
                "tenant, or confirm this is a different person or company.",
                conflicts=[m.model_dump(mode="json") for m in matches],
            )
        return TenantService(self.session, self.actor).create(values), True

    def _existing_contract(
        self, tenant: Tenant, assignment: dict[str, Any]
    ) -> TenantOccupancy | None:
        """The same request made before (a retry): reuse its contract."""
        return self.session.scalar(
            select(TenantOccupancy)
            .where(
                TenantOccupancy.tenant_id == tenant.id,
                TenantOccupancy.space_id == assignment["space_ids"][0],
                TenantOccupancy.start_date == assignment["start_date"],
                TenantOccupancy.status == ContractStatus.ACTIVE,
            )
            .with_for_update()
        )

    def _contracts(
        self, tenant: Tenant, assignment: dict[str, Any], quote: AssignmentQuote
    ) -> TenantOccupancy:
        """One ACTIVE contract per chosen space (a several-seat contract shares a group),
        through the contract service, so the allocation engine checks every space. Each
        seat's rent and deposit come from its own pricing rule. Returns the first."""
        from app.services.contracts import ContractService

        keys = (
            "contract_type",
            "lock_in_period",
            "notice_period",
            "payment_due_day",
            "tds_percent",
        )
        terms = {k: assignment[k] for k in keys if assignment.get(k) is not None}
        made: list[TenantOccupancy] = []
        for space_id in assignment["space_ids"]:
            rent = deposit = None
            currency = "INR"
            if quote.pricing is not None:
                _, estimate = PricingService(self.session).estimate(
                    space_id, BillingPeriod.MONTHLY, on=assignment["start_date"]
                )
                rent = _money(estimate.rent)
                deposit = _money(estimate.deposit)
                currency = estimate.currency
            read = ContractService(self.session, self.actor).create(
                {
                    "tenant_id": tenant.id,
                    "property_id": assignment["property_id"],
                    "space_id": space_id,
                    "start_date": assignment["start_date"],
                    "end_date": assignment.get("end_date"),
                    "monthly_rent": rent,
                    "security_deposit": deposit,
                    "currency": currency,
                    "activate": True,
                    **terms,
                }
            )
            contract = self.session.get(TenantOccupancy, read.id)
            assert contract is not None
            made.append(contract)
        if len(made) > 1:
            group = uuid.uuid4()
            for contract in made:
                contract.group_id = group
            self.session.flush()
        return made[0]

    def _open_account(self, tenant: Tenant) -> None:
        new_id = self.session.scalar(
            insert(Account)
            .values(tenant_id=tenant.id, created_by=self.actor.id)
            .on_conflict_do_nothing(index_elements=["tenant_id"])
            .returning(Account.id)
        )
        if new_id is not None:
            log_activity(
                self.session,
                self.actor,
                action="opened",
                entity_type="accounts",
                entity_id=new_id,
                property_id=None,
                message=f"Account opened for {tenant.name}: added with a space by the "
                "property team."[:500],
            )

    def _initial_invoice(
        self, tenant: Tenant, contract: TenantOccupancy, quote: AssignmentQuote
    ) -> tuple[uuid.UUID | None, str | None]:
        from app.services.invoices import InvoiceService

        if quote.invoice is None:
            return None, (
                f"No invoice was made: {quote.pricing_missing} Add a monthly price for this "
                "space, then create the invoice from Accounts."
            )
        preview = quote.invoice
        existing = self.session.scalar(
            select(Invoice.id).where(
                Invoice.contract_id == contract.id,
                Invoice.period_start == preview.period_start,
                Invoice.status != InvoiceStatus.CANCELLED,
            )
        )
        if existing is not None:
            return existing, None
        account_id = self.session.scalar(select(Account.id).where(Account.tenant_id == tenant.id))
        invoices = InvoiceService(self.session, self.actor)
        draft = invoices.create(
            {
                "account_id": account_id,
                "contract_id": contract.id,
                "invoice_date": business_today(),
                "period_start": preview.period_start,
                "period_end": preview.period_end,
                "currency": preview.currency,
                "notes": INITIAL_NOTE.format(reference=contract.reference),
            }
        )
        invoices.set_lines(
            draft.id,
            [
                {"category": line.category, "description": line.description, "amount": line.amount}
                for line in preview.lines
            ],
        )
        return draft.id, None

    def _storage(self) -> ObjectStorage:
        if self._storage_factory is None:
            raise AppError("File storage is not configured.")
        return self._storage_factory()
