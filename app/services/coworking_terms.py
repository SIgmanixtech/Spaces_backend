"""Values for the Coworking License Agreement (owner decisions of 17 Nov 2026).

Every value comes from records, never from a default:

- member: the tenant (name, company, email, phone, personal PAN, company PAN, CIN, GSTIN,
  registered address, representative);
- workspace: the contract and the other seats of a several-seat contract (quantity),
  their property, building, floor and space;
- money: the membership fee is the contract's agreed monthly amount (summed over its seats;
  the property team may change it on the contract, which is logged). Maintenance, GST and
  additional services come from each seat's pricing rule for the start date. TDS is the
  percentage the administrator entered on the agreement; empty means none (never assumed);
- dates and terms: start and end dates, the fee due day, lock-in and notice periods as
  entered on the contract;
- licensor: the property's company profile, bank and UPI details included.

A value that is missing stays None, so the agreement engine lists it instead of printing a
guess. Amounts are rounded to the paisa, half up, line by line.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.company import CompanyProfile
from app.models.enums import BillingPeriod, TaxTreatment
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.services.amount_words import rupees_in_words

CENT = Decimal("0.01")

# What a coworking template may contain (owner's list), with labels for "missing" messages.
COWORKING_PLACEHOLDERS: dict[str, str] = {
    # Member
    "member_name": "Member name",
    "company_name": "Member company",
    "member_email": "Member email",
    "member_phone": "Member phone",
    "member_pan": "Member's personal PAN",
    "company_pan": "Member company's PAN",
    "company_cin": "Member company's CIN",
    "gstin": "Member GSTIN",
    "registered_address": "Member's registered address",
    "representative_name": "Member representative",
    "representative_designation": "Member representative's designation",
    # Property and inventory
    "property_name": "Property name",
    "property_address": "Property address",
    "building_name": "Building",
    "floor_name": "Floor",
    "space_name": "Space or workstations",
    "space_category": "Space category",
    "space_area": "Space area (sq ft)",
    "membership_type": "Membership type",
    "membership_quantity": "Membership quantity",
    # Money
    "membership_fee": "Membership fee (agreed monthly amount on the contract)",
    "maintenance_amount": "Maintenance (from the pricing rule)",
    "gst_percentage": "GST percentage (from the pricing rule)",
    "gst_amount": "GST amount",
    "tds_percentage": "TDS percentage (entered on the agreement)",
    "tds_amount": "TDS amount",
    "subtotal_amount": "Amount before GST",
    "invoice_amount": "Invoice amount",
    "net_payable_amount": "Net payable amount",
    "amount_in_words": "Net payable amount in words",
    "security_deposit": "Security deposit (on the contract)",
    "security_deposit_words": "Security deposit in words",
    # Agreement
    "agreement_number": "Agreement number",
    "agreement_start_date": "Membership start date",
    "agreement_end_date": "Membership end date",
    "billing_frequency": "Billing frequency",
    "payment_due_date": "Fee due day of the month (entered on the contract)",
    "lock_in_period": "Lock-in period (entered on the contract)",
    "notice_period": "Notice period (entered on the contract)",
    "agreement_generation_date": "Date generated",
    # Licensor
    "provider_company_name": "Licensor company name",
    "provider_address": "Licensor address",
    "provider_pan": "Licensor PAN",
    "provider_cin": "Licensor CIN",
    "provider_gstin": "Licensor GSTIN",
    "provider_phone": "Licensor phone",
    "provider_email": "Licensor email",
    "provider_signatory_name": "Licensor signatory",
    "provider_signatory_designation": "Licensor signatory's designation",
    # Bank (the licensor's account, never the member's)
    "bank_name": "Bank name",
    "bank_account_name": "Bank account name",
    "bank_account_number": "Bank account number",
    "bank_ifsc": "Bank IFSC",
    "bank_branch": "Bank branch",
    "upi_id": "UPI ID",
    # Rows of the membership table ({%tr for item in membership_items %})
    "item.number": "Table row: serial number",
    "item.membership": "Table row: membership",
    "item.quantity": "Table row: quantity",
    "item.nt_amount": "Table row: amount before GST",
    "item.gst_amount": "Table row: GST",
    "item.invoice_amount": "Table row: invoice amount",
    "item.tds_amount": "Table row: TDS",
    "item.net_amount": "Table row: amount to be received",
}

# Printed as empty when there is no value: an individual member has no company, PAN details
# are not always held, and a signature block may be completed by hand.
OPTIONAL = {
    "company_name",
    "company_pan",
    "company_cin",
    "gstin",
    "member_pan",
    "member_email",
    "member_phone",
    "representative_designation",
    "space_area",
    "agreement_end_date",
    "building_name",
    "floor_name",
    "provider_pan",
    "provider_cin",
    "provider_gstin",
    "provider_phone",
    "provider_email",
    "provider_signatory_designation",
}


def ordinal_day(day: int) -> str:
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def long_date(value: date | None) -> str | None:
    """29th May 2026, as the approved agreement writes dates."""
    return f"{ordinal_day(value.day)} {value:%B %Y}" if value else None


def money(value: Decimal | None) -> str | None:
    """Indian grouping: 1,15,500.00."""
    if value is None:
        return None
    whole, _, cents = f"{Decimal(value):.2f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail]) + "." + cents


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = f"{Decimal(value).normalize():f}"
    return text


def coworking_values(
    session: Session,
    contract: TenantOccupancy,
    members: list[TenantOccupancy],
    *,
    prop: Any,
    building: Any,
    floor: Any,
    profile: CompanyProfile | None,
    premises: str,
    address: str | None,
    generated_on: date,
) -> dict[str, Any]:
    from app.services.pricing import PricingService

    tenant = session.get(Tenant, contract.tenant_id)
    spaces = [session.get(Space, m.space_id) for m in members if m.space_id]
    spaces = [s for s in spaces if s is not None]
    first = spaces[0] if spaces else None
    type_label = (
        str(first.space_type).replace("_", " ").title() if first is not None else "Whole property"
    )

    # Money: the agreed fee from the contract; the rest from each seat's pricing rule.
    rents = [m.monthly_rent for m in members]
    fee = sum(rents, Decimal(0)) if rents and all(r is not None for r in rents) else None
    maintenance: Decimal | None = Decimal(0)
    rates: set[Decimal] = set()
    treatment: TaxTreatment | None = None
    extras: OrderedDict[str, Decimal] = OrderedDict()
    for space in spaces:
        try:
            price, estimate = PricingService(session).estimate(
                space.id, BillingPeriod.MONTHLY, on=contract.start_date
            )
        except AppError:
            maintenance, treatment = None, None
            rates.clear()
            break
        rule = price.rule
        maintenance = (maintenance or Decimal(0)) + Decimal(str(estimate.maintenance))
        treatment = TaxTreatment(rule.tax_treatment)
        rates.add(
            Decimal(str(rule.tax_rate or 0)) if treatment != TaxTreatment.EXEMPT else Decimal(0)
        )
        for charge in rule.additional_charges or []:
            label = (
                getattr(charge, "label", None)
                or getattr(charge, "name", None)
                or "Additional service"
            )
            amount = Decimal(str(getattr(charge, "amount", 0)))
            extras[label] = extras.get(label, Decimal(0)) + amount
    gst_rate = rates.pop() if len(rates) == 1 else None  # seats priced at different GST: ask
    tds_rate = Decimal(contract.tds_percent) if contract.tds_percent is not None else Decimal(0)

    def line(label: str, quantity: str, gross: Decimal) -> dict[str, Any]:
        assert gst_rate is not None
        if treatment == TaxTreatment.INCLUDED:
            nt = _round(gross / (1 + gst_rate / 100))
            gst = gross - nt
        else:
            nt = _round(gross)
            gst = _round(nt * gst_rate / 100)
        invoice = nt + gst
        tds = _round(nt * tds_rate / 100)
        return {
            "membership": label,
            "quantity": quantity,
            "nt": nt,
            "gst": gst,
            "invoice": invoice,
            "tds": tds,
            "net": invoice - tds,
        }

    rows: list[dict[str, Any]] = []
    if fee is not None and gst_rate is not None and maintenance is not None:
        rows.append(
            line(f"{type_label}s" if len(spaces) > 1 else type_label, str(len(members)), fee)
        )
        if maintenance > 0:
            rows.append(line("Maintenance", "", maintenance))
        for label, amount in extras.items():
            rows.append(line(label, "1", amount))

    def total(key: str) -> Decimal | None:
        return sum((r[key] for r in rows), Decimal(0)) if rows else None

    deposits = [m.security_deposit for m in members if m.security_deposit is not None]
    deposit = sum(deposits, Decimal(0)) if deposits else None
    net = total("net")
    pick = lambda attr: getattr(profile, attr) if profile else None  # noqa: E731
    representative = (tenant.signatory_name or tenant.name) if tenant else None
    return {
        "member_name": tenant.name if tenant else None,
        "company_name": tenant.company_name if tenant else None,
        "member_email": tenant.email if tenant else None,
        "member_phone": tenant.phone if tenant else None,
        "member_pan": tenant.personal_pan if tenant else None,
        "company_pan": tenant.pan if tenant else None,
        "company_cin": tenant.cin if tenant else None,
        "gstin": tenant.gstin if tenant else None,
        "registered_address": tenant.registered_address if tenant else None,
        "representative_name": representative,
        "representative_designation": tenant.signatory_designation if tenant else None,
        "property_name": prop.name if prop else None,
        "property_address": address,
        "building_name": building.name if building else None,
        "floor_name": floor.name if floor else None,
        "space_name": premises,
        "space_category": type_label,
        "space_area": (
            f"{sum(s.area_sqft for s in spaces):,}"
            if spaces and all(s.area_sqft for s in spaces)
            else None
        ),
        "membership_type": type_label,
        "membership_quantity": str(len(members)),
        "membership_fee": money(fee),
        "maintenance_amount": money(maintenance) if rows else None,
        "gst_percentage": _percent(gst_rate) if rows else None,
        "gst_amount": money(total("gst")),
        "tds_percentage": _percent(tds_rate),
        "tds_amount": money(total("tds")),
        "subtotal_amount": money(total("nt")),
        "invoice_amount": money(total("invoice")),
        "net_payable_amount": money(net),
        "amount_in_words": rupees_in_words(net) if net is not None else None,
        "security_deposit": money(deposit),
        "security_deposit_words": rupees_in_words(deposit) if deposit is not None else None,
        "agreement_number": contract.reference,
        "agreement_start_date": long_date(contract.start_date),
        "agreement_end_date": long_date(contract.end_date),
        "billing_frequency": "Monthly",
        "payment_due_date": (
            ordinal_day(contract.payment_due_day) if contract.payment_due_day else None
        ),
        "lock_in_period": contract.lock_in_period,
        "notice_period": contract.notice_period,
        "agreement_generation_date": long_date(generated_on),
        "provider_company_name": pick("legal_name"),
        "provider_address": pick("registered_address"),
        "provider_pan": pick("pan"),
        "provider_cin": pick("cin"),
        "provider_gstin": pick("gstin"),
        "provider_phone": pick("phone"),
        "provider_email": pick("email"),
        "provider_signatory_name": pick("signatory_name"),
        "provider_signatory_designation": pick("signatory_designation"),
        "bank_name": pick("bank_name"),
        "bank_account_name": pick("bank_account_name"),
        "bank_account_number": pick("bank_account_number"),
        "bank_ifsc": pick("bank_ifsc"),
        "bank_branch": pick("bank_branch"),
        "upi_id": pick("upi_id"),
        # The table rows, printed with "₹ " in the template.
        "membership_items": [
            {
                "number": str(i),
                "membership": r["membership"],
                "quantity": r["quantity"],
                "nt_amount": money(r["nt"]),
                "gst_amount": money(r["gst"]),
                "invoice_amount": money(r["invoice"]),
                "tds_amount": money(r["tds"]),
                "net_amount": money(r["net"]),
            }
            for i, r in enumerate(rows, start=1)
        ],
    }
