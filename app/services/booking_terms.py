"""Values for a one-time booking's agreement (owner request of 19 Nov 2026, migration 0027).

Every value comes from records or from what the administrator typed when generating:

- member: the booking's tenant (company, PANs, CIN, GSTIN, registered address,
  representative). The Aadhaar's last four digits and the residential address are optional
  and typed on the generate form; they are printed as given and stored only with the
  document;
- place: the booking's property, building, floor, portion and space;
- time: the booking's start and end, in India time;
- money: the agreed fee typed on the generate form (prefilled from the booking's quote),
  its GST treatment and rate, and TDS (empty means none, never assumed);
- licensor: the property's company profile, bank and UPI details included.

A missing value stays None so the caller lists it instead of printing a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.services.amount_words import rupees_in_words
from app.services.coworking_terms import money

IST = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")
GST_TREATMENTS = ("INCLUDED", "ADDITIONAL", "EXEMPT")

BOOKING_PLACEHOLDERS: dict[str, str] = {
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
    "member_aadhaar": "Member's Aadhaar (masked, last four digits)",
    "residential_address": "Member's residential address",
    "member_clause": "The 'You' sentence: the company, then the person representing it",
    # Place
    "property_name": "Property name",
    "property_address": "Property address",
    "building_name": "Building",
    "floor_name": "Floor",
    "portion_name": "Portion",
    "space_name": "Space",
    "space_category": "Space type",
    "space_capacity": "Space capacity (seats)",
    # Booking
    "booking_reference": "Booking reference",
    "booking_start": "Booking start (date and time, IST)",
    "booking_end": "Booking end (date and time, IST)",
    "booking_period": "Booking period, start to end (IST)",
    "booking_hours": "Booking length (e.g. 4 hours)",
    "agreement_number": "Agreement reference",
    "agreement_generation_date": "Date generated",
    # Money
    "fee_sentence": "The fee sentence (amount, GST)",
    "gst_percentage": "GST percentage",
    "gst_amount": "GST amount",
    "tds_percentage": "TDS percentage",
    "tds_amount": "TDS amount",
    "subtotal_amount": "Amount before GST",
    "invoice_amount": "Invoice amount",
    "net_payable_amount": "Net payable amount",
    "amount_in_words": "Net payable amount in words",
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
    # Bank (the licensor's account)
    "bank_name": "Bank name",
    "bank_account_name": "Bank account name",
    "bank_account_number": "Bank account number",
    "bank_ifsc": "Bank IFSC",
    "bank_branch": "Bank branch",
    "upi_id": "UPI ID",
    # Rows of the fee table ({%tr for item in membership_items %})
    "item.number": "Table row: serial number",
    "item.membership": "Table row: what is booked",
    "item.quantity": "Table row: quantity",
    "item.nt_amount": "Table row: amount before GST",
    "item.gst_amount": "Table row: GST",
    "item.invoice_amount": "Table row: invoice amount",
    "item.tds_amount": "Table row: TDS",
    "item.net_amount": "Table row: amount to be received",
}

# Printed as empty when there is no value.
OPTIONAL = {
    "company_name",
    "member_email",
    "member_phone",
    "member_pan",
    "company_pan",
    "company_cin",
    "gstin",
    "representative_designation",
    "member_aadhaar",
    "residential_address",
    "building_name",
    "floor_name",
    "portion_name",
    "space_capacity",
    "provider_pan",
    "provider_cin",
    "provider_gstin",
    "provider_phone",
    "provider_email",
    "provider_signatory_designation",
}


@dataclass(frozen=True)
class Fee:
    """What the administrator typed: the agreed amount and how GST applies to it."""

    amount: Decimal
    gst_treatment: str  # INCLUDED: the amount includes GST; ADDITIONAL: GST on top; EXEMPT
    gst_rate: Decimal
    tds_rate: Decimal | None = None


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> str:
    return f"{value.normalize():f}"


def when(moment: datetime) -> str:
    """11:00 PM, Tuesday, 17 November 2026 (India time)."""
    local = moment.astimezone(IST)
    return f"{local:%I:%M %p}".lstrip("0") + f", {local:%A}, {local.day} {local:%B %Y}"


def duration(start: datetime, end: datetime) -> str:
    minutes = int((end - start).total_seconds() // 60)
    hours, rest = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if rest:
        parts.append(f"{rest} minute{'s' if rest != 1 else ''}")
    return " ".join(parts) or "0 minutes"


def fee_lines(fee: Fee) -> dict[str, Decimal]:
    rate = Decimal(0) if fee.gst_treatment == "EXEMPT" else fee.gst_rate
    if fee.gst_treatment == "INCLUDED":
        nt = _round(fee.amount * 100 / (100 + rate))
        gst = fee.amount - nt
    else:
        nt = _round(fee.amount)
        gst = _round(nt * rate / 100)
    tds = _round(nt * (fee.tds_rate or Decimal(0)) / 100)
    return {
        "nt": nt,
        "gst": gst,
        "invoice": nt + gst,
        "tds": tds,
        "net": nt + gst - tds,
        "rate": rate,
    }


def member_clause(tenant: Any, aadhaar: str | None, residence: str | None) -> str | None:
    """The 'You' sentence (owner, 17 Nov 2026): the company and its details first, then the
    person representing it; an individual is named directly."""
    if tenant is None:
        return None
    person_ids = ", ".join(
        x
        for x in (
            f"PAN {tenant.personal_pan}" if tenant.personal_pan else "",
            f"Aadhaar {aadhaar}" if aadhaar else "",
        )
        if x
    )
    person = (tenant.signatory_name or tenant.name) + (f" ({person_ids})" if person_ids else "")
    residing = f", residing at {residence}" if residence else ""
    if tenant.company_name:
        details = ", ".join(
            x
            for x in (
                f"CIN {tenant.cin}" if tenant.cin else "",
                f"PAN {tenant.pan}" if tenant.pan else "",
                f"GSTIN {tenant.gstin}" if tenant.gstin else "",
            )
            if x
        )
        office = (
            f", having its registered office at {tenant.registered_address}"
            if tenant.registered_address
            else ""
        )
        return (
            f"means 'Member' or '{tenant.company_name}'"
            + (f" ({details})" if details else "")
            + f"{office}, represented by {person}{residing}."
        )
    address = residing or (
        f", residing at {tenant.registered_address}" if tenant.registered_address else ""
    )
    return f"means 'Member' or '{person}'{address}."


def booking_values(
    *,
    booking: Any,
    tenant: Any,
    prop: Any,
    building: Any,
    floor: Any,
    portion: Any,
    space: Any,
    profile: Any,
    fee: Fee | None,
    reference: str,
    generated_on: Any,
    aadhaar_last4: str | None = None,
    residence: str | None = None,
) -> dict[str, Any]:
    aadhaar = f"XXXX XXXX {aadhaar_last4}" if aadhaar_last4 else None
    pick = lambda attr: getattr(profile, attr) if profile else None  # noqa: E731
    lines = fee_lines(fee) if fee else None
    type_label = str(space.space_type).replace("_", " ").title() if space else None
    where = ", ".join(
        x
        for x in (
            f"Portion {portion.name}" if portion is not None else "",
            floor.name if floor is not None else "",
        )
        if x
    )
    seats = f" ({space.capacity} seats)" if space is not None and space.capacity else ""
    item_label = (
        f"{space.name}{' - ' + where if where else ''}{seats}, one-time booking {booking.reference}"
        if space is not None
        else None
    )
    address = (
        ", ".join(p for p in (prop.address, prop.city, prop.state, prop.postal_code) if p)
        if prop
        else None
    )
    hours = duration(booking.start_at, booking.end_at)
    fee_sentence = None
    if fee and lines:
        if lines["rate"] == 0:
            fee_sentence = (
                f"The fee for this booking is Rs. {money(lines['invoice'])} (no GST is "
                "charged), as shown in the table above."
            )
        else:
            fee_sentence = (
                f"The fee for this booking is Rs. {money(lines['invoice'])} including GST "
                f"(Rs. {money(lines['nt'])} plus GST {_percent(lines['rate'])}% Rs. "
                f"{money(lines['gst'])}), as shown in the table above."
            )
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
        "representative_name": (tenant.signatory_name or tenant.name) if tenant else None,
        "representative_designation": tenant.signatory_designation if tenant else None,
        "member_aadhaar": aadhaar,
        "residential_address": residence,
        "member_clause": member_clause(tenant, aadhaar, residence),
        "property_name": prop.name if prop else None,
        "property_address": address,
        "building_name": building.name if building else None,
        "floor_name": floor.name if floor else None,
        "portion_name": portion.name if portion else None,
        "space_name": space.name if space else None,
        "space_category": type_label,
        "space_capacity": str(space.capacity) if space is not None and space.capacity else None,
        "booking_reference": booking.reference,
        "booking_start": when(booking.start_at),
        "booking_end": when(booking.end_at),
        "booking_period": (
            f"from {when(booking.start_at)} to {when(booking.end_at)} IST ({hours}), as one "
            "continuous booking"
        ),
        "booking_hours": hours,
        "agreement_number": reference,
        "agreement_generation_date": f"{generated_on.day} {generated_on:%B %Y}",
        "fee_sentence": fee_sentence,
        "gst_percentage": _percent(lines["rate"]) if lines else None,
        "gst_amount": money(lines["gst"]) if lines else None,
        "tds_percentage": _percent(fee.tds_rate or Decimal(0)) if fee else None,
        "tds_amount": money(lines["tds"]) if lines else None,
        "subtotal_amount": money(lines["nt"]) if lines else None,
        "invoice_amount": money(lines["invoice"]) if lines else None,
        "net_payable_amount": money(lines["net"]) if lines else None,
        "amount_in_words": rupees_in_words(lines["net"]) if lines else None,
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
        "membership_items": (
            [
                {
                    "number": "1",
                    "membership": item_label,
                    "quantity": "1",
                    "nt_amount": money(lines["nt"]),
                    "gst_amount": money(lines["gst"]),
                    "invoice_amount": money(lines["invoice"]),
                    "tds_amount": money(lines["tds"]),
                    "net_amount": money(lines["net"]),
                }
            ]
            if lines and item_label
            else []
        ),
    }
