# ruff: noqa: F811  (pytest fixtures shared from test_client_api and test_agreements)
"""Phase 4B: company profiles, prefill, template fields, amount in words, generation while
PENDING (admins only), signed copies and contracts from accepted proposals.

The first tests need no database; the rest need the migrated test database (like every
API test here)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError
from app.models import Allocation, Booking, Tenant, TenantOccupancy
from app.services.agreements import clean_custom_fields
from app.services.amount_words import indian_words, rupees_in_words
from app.services.occupancy import business_today
from app.storage import LocalStorage
from tests.test_agreements import (
    LICENCE_TEXT,
    SIGNED_PDF,
    make_docx,
    sign_with_copy,
    storage,  # noqa: F401  (fixture)
    text_of,
    upload,
)
from tests.test_client_api import CUSTOMER, MONDAY, book, hub, priya, ravi  # noqa: F401
from tests.test_crm import CRM, lead
from tests.test_resources import V1

ADMIN = f"{V1}/admin"
PROFILE = {
    "legal_name": "Sigmanix Spaces Pvt Ltd",
    "registered_address": "Plot 7, Madhapur, Hyderabad 500081",
    "signatory_name": "Kiran Rao",
    "signatory_designation": "Director",
    "gstin": "36AAACS1234F1Z5",
    "pan": "AAACS1234F",
    "cin": "U70200TG2020PTC140000",
}
COMPANY_TEXT = [
    "LEASE {{ contract.reference }} between {{ company.legal_name }} ({{ company.cin }})",
    "and {{ customer.legal_name }}, GSTIN {{ customer.gstin }}.",
    "Rent {{ lease.rent }} ({{ lease.rent_words }}). Lock-in: {{ custom.lock_in }}.",
    "Notice: {{ custom.notice }}.",
]


# --- amount in words and field rules (no database) -----------------------------------------


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        ("0", "Rupees Zero Only"),
        ("1", "Rupees One Only"),
        ("19", "Rupees Nineteen Only"),
        ("99", "Rupees Ninety Nine Only"),
        ("100", "Rupees One Hundred Only"),
        ("1000", "Rupees One Thousand Only"),
        ("100000", "Rupees One Lakh Only"),
        ("115500.50", "Rupees One Lakh Fifteen Thousand Five Hundred and Fifty Paise Only"),
        ("2500000", "Rupees Twenty Five Lakh Only"),
        ("10000000", "Rupees One Crore Only"),
        (
            "123456789.05",
            "Rupees Twelve Crore Thirty Four Lakh Fifty Six Thousand Seven "
            "Hundred Eighty Nine and Five Paise Only",
        ),
        ("0.75", "Rupees Zero and Seventy Five Paise Only"),
        ("1.005", "Rupees One and One Paise Only"),  # half up to whole paise
        ("1.004", "Rupees One Only"),
        ("1000000000", "Rupees One Hundred Crore Only"),
    ],
)
def test_rupees_in_words_uses_indian_numbering(amount: str, words: str) -> None:
    assert rupees_in_words(Decimal(amount)) == words


def test_negative_amounts_are_refused() -> None:
    with pytest.raises(ValueError):
        rupees_in_words(Decimal("-1"))
    assert indian_words(0) == "Zero"


def test_custom_fields_are_checked() -> None:
    assert clean_custom_fields(
        [{"key": "lock_in", "label": " Lock-in  period ", "type": "TEXT", "required": 1}]
    ) == [{"key": "lock_in", "label": "Lock-in period", "type": "text", "required": True}]
    for bad in (
        [{"key": "Lock", "label": "x"}],
        [{"key": "a", "label": ""}],
        [{"key": "a", "label": "x", "type": "money"}],
        [{"key": "a", "label": "x"}, {"key": "a", "label": "y"}],
        [{"key": "other_charges", "label": "x"}],  # reserved
    ):
        with pytest.raises(BadRequestError):
            clean_custom_fields(bad)


# --- helpers ---------------------------------------------------------------------------------


def profile_for(api: TestClient, property_id: str, **over: object) -> dict:
    made = api.post(f"{ADMIN}/company-profiles", json={**PROFILE, **over})
    assert made.status_code == 201, made.text
    linked = api.put(
        f"{ADMIN}/company-profiles/{made.json()['id']}/properties",
        json={"property_ids": [property_id]},
    )
    assert linked.status_code == 200, linked.text
    return linked.json()


def pending_contract(api: TestClient, hub: dict, **legal: object) -> dict:
    tenant = api.post(
        f"{ADMIN}/tenants",
        json={"name": "Acme Ltd", "company_name": "Acme Ltd", "email": "ops@acme.in", **legal},
    ).json()
    made = api.post(
        f"{ADMIN}/contracts",
        json={
            "tenant_id": tenant["id"],
            "property_id": hub["property"]["id"],
            "space_id": hub["cabin"]["id"],
            "start_date": str((MONDAY + timedelta(days=40)).date()),
            "monthly_rent": "115500.50",
            "security_deposit": "300000",
        },
    )
    assert made.status_code == 201, made.text
    contract = made.json()
    api.patch(f"{ADMIN}/contracts/{contract['id']}", json={"contract_type": "SPACE_LEASE"})
    return contract


def company_template(api: TestClient) -> dict:
    response = api.post(
        f"{V1}/contract-templates",
        data={
            "contract_type": "SPACE_LEASE",
            "name": "Lease with company",
            "required_fields": "legal_name,gstin",
            "custom_fields": '[{"key": "lock_in", "label": "Lock-in period", "type": "text",'
            ' "required": true}, {"key": "notice", "label": "Notice (months)",'
            ' "type": "number", "required": false}]',
        },
        files={"file": ("t.docx", make_docx(COMPANY_TEXT), "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["unknown_placeholders"] == [] and template["unused_custom_fields"] == []
    assert api.post(f"{V1}/contract-templates/{template['id']}/approve").status_code == 200
    return template


def holds(db_session: Session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(Allocation)) or 0


def body_of(storage: LocalStorage, link: str) -> str:
    path = link.split("/files/local/")[1].split("?")[0].replace("%2F", "/")
    return text_of(storage.get(path))


# --- company profiles -----------------------------------------------------------------------


def test_company_profiles_are_admin_only_and_history_hides_values(
    api: TestClient, priya: TestClient, hub: dict
) -> None:
    assert priya.get(f"{ADMIN}/company-profiles").status_code == 403
    assert api.post(f"{ADMIN}/company-profiles", json={"legal_name": ""}).status_code == 422
    assert (
        api.post(f"{ADMIN}/company-profiles", json={"legal_name": "X", "pan": "12"}).status_code
        == 422
    )
    profile = profile_for(api, hub["property"]["id"], gstin="36aaacs1234f1z5")
    assert profile["gstin"] == "36AAACS1234F1Z5"
    assert [p["name"] for p in profile["properties"]] == ["Sigma Hub"]
    changed = api.patch(
        f"{ADMIN}/company-profiles/{profile['id']}", json={"pan": "BBBCS1234F", "cin": None}
    )
    assert changed.status_code == 200 and changed.json()["cin"] is None
    history = api.get(
        f"{ADMIN}/activity",
        params={"entity_type": "company_profiles", "entity_id": profile["id"]},
    ).json()["items"]
    text = " ".join(e["message"] for e in history)
    assert "PAN" in text and "CIN" in text
    for value in ("BBBCS1234F", PROFILE["cin"], PROFILE["gstin"]):
        assert value not in text
    # A property belongs to at most one profile: assigning it elsewhere moves it.
    other = api.post(f"{ADMIN}/company-profiles", json={"legal_name": "Other LLP"}).json()
    moved = api.put(
        f"{ADMIN}/company-profiles/{other['id']}/properties",
        json={"property_ids": [hub["property"]["id"]]},
    ).json()
    assert [p["name"] for p in moved["properties"]] == ["Sigma Hub"]
    assert api.get(f"{ADMIN}/company-profiles/{profile['id']}").json()["properties"] == []
    assert (
        api.put(
            f"{ADMIN}/company-profiles/{other['id']}/properties",
            json={"property_ids": ["00000000-0000-0000-0000-000000000000"]},
        ).status_code
        == 400
    )


# --- Option A: admins generate while PENDING; nothing is held or activated ----------------------


def test_admin_generates_while_pending_without_holding_or_activating(
    api: TestClient, hub: dict, storage: LocalStorage, db_session: Session
) -> None:
    profile = profile_for(api, hub["property"]["id"])
    company_template(api)
    contract = pending_contract(
        api,
        hub,
        registered_address="1 MG Road, Hyderabad",
        gstin="36ABCDE1234F1Z5",
        signatory_name="Asha Rao",
    )
    url = f"{ADMIN}/contracts/{contract['id']}/agreement"
    before = holds(db_session)
    plan = api.get(url).json()
    assert plan["lifecycle"] == "Draft" and plan["contract_status"] == "PENDING"
    assert plan["company_profile"]["legal_name"] == PROFILE["legal_name"]
    assert "Lock-in period" in plan["missing"]

    # Prefill is explicit: a preview first, nothing saved until applied.
    preview = {r["key"]: r for r in api.get(f"{url}/prefill").json()}
    assert preview["gstin"]["source"] == "36ABCDE1234F1Z5" and preview["gstin"]["differs"]
    assert preview["address"]["source"] == "1 MG Road, Hyderabad"
    assert api.get(url).json()["fields"][0]["value"] is None
    applied = api.post(f"{url}/prefill", json={"keys": ["legal_name", "gstin", "address"]})
    assert applied.status_code == 200, applied.text
    saved = {f["key"]: f["value"] for f in applied.json()["fields"]}
    assert saved == {"legal_name": "Acme Ltd", "gstin": "36ABCDE1234F1Z5"}

    # Required template field, checked before generation.
    refused = api.post(f"{url}/documents")
    assert refused.status_code == 400 and "Lock-in period" in refused.json()["detail"]
    bad = api.put(f"{url}/fields", json={"values": {"notice": "three"}})
    assert bad.status_code == 400
    filled = api.put(f"{url}/fields", json={"values": {"lock_in": "11 months"}})
    assert filled.status_code == 200, filled.text
    made = api.post(f"{url}/documents")
    assert made.status_code == 201, made.text

    # Generating held nothing and approved nothing.
    after = api.get(url).json()
    assert after["contract_status"] == "PENDING"
    assert after["lifecycle"] == "Generated - pending review"
    assert holds(db_session) == before
    assert db_session.get(TenantOccupancy, contract["id"]).status == "PENDING"

    # The document's values are a snapshot: amount in words, company, tenant details.
    values = after["documents"][0]["values"]
    assert values["lease.rent_words"] == (
        "Rupees One Lakh Fifteen Thousand Five Hundred and Fifty Paise Only"
    )
    assert values["company.legal_name"] == PROFILE["legal_name"]
    assert values["custom.notice"] == ""  # optional, left empty: prints nothing
    link = api.get(f"{ADMIN}/contract-documents/{made.json()['id']}/download").json()["url"]
    text = body_of(storage, link)
    assert PROFILE["legal_name"] in text and "11 months" in text and "{{" not in text

    # Later edits to the profile and tenant never change the generated document.
    api.patch(f"{ADMIN}/company-profiles/{profile['id']}", json={"legal_name": "Renamed Ltd"})
    api.patch(f"{ADMIN}/tenants/{contract['tenant_id']}", json={"gstin": "36PQRST6789K1Z5"})
    kept = api.get(url).json()["documents"][0]["values"]
    assert kept["company.legal_name"] == PROFILE["legal_name"]
    assert kept["customer.gstin"] == "36ABCDE1234F1Z5"
    assert PROFILE["legal_name"] in body_of(storage, link)


def test_customers_still_need_an_approved_contract(
    api: TestClient, priya: TestClient, hub: dict, storage: LocalStorage
) -> None:
    mine = book(priya, hub["seat2"])
    start = (MONDAY + timedelta(days=3)).date()
    request = priya.post(
        f"{CUSTOMER}/bookings/{mine['id']}/contract", json={"start_date": str(start), "months": 1}
    ).json()
    api.patch(
        f"{V1}/contracts/{request['id']}",
        json={"contract_type": "COWORKING_LICENCE", "monthly_rent": "5000"},
    )
    template = upload(api, make_docx(LICENCE_TEXT), "COWORKING_LICENCE")
    api.post(f"{V1}/contract-templates/{template['id']}/approve")
    url = f"{CUSTOMER}/contracts/{request['id']}/agreement"
    priya.put(f"{url}/details", json={"values": {"legal_name": "Priya Sharma"}})
    plan = priya.get(url).json()
    assert "must approve the contract" in plan["problem"]
    assert priya.post(f"{url}/documents").status_code == 409
    # The customer's view carries no admin-only data.
    assert "lifecycle" not in plan and "company_profile" not in plan
    assert api.post(f"{V1}/contracts/{request['id']}/activate").status_code == 200
    made = priya.post(f"{url}/documents")
    assert made.status_code == 201, made.text
    assert "values" not in made.json() and "signed_copy" not in made.json()


# --- signed copies --------------------------------------------------------------------------


def test_signed_copy_upload_marks_signed_without_activating(
    api: TestClient,
    priya: TestClient,
    hub: dict,
    storage: LocalStorage,
    db_session: Session,
) -> None:
    profile_for(api, hub["property"]["id"])
    company_template(api)
    contract = pending_contract(api, hub, gstin="36ABCDE1234F1Z5")
    url = f"{ADMIN}/contracts/{contract['id']}/agreement"
    api.post(f"{url}/prefill", json={"keys": ["legal_name", "gstin"]})
    api.put(f"{url}/fields", json={"values": {"lock_in": "11 months"}})
    document = api.post(f"{url}/documents").json()
    doc_url = f"{ADMIN}/contract-documents/{document['id']}"

    # Not before it is approved; the plain sign step is retired.
    sign_with_copy(api, document["id"], expected=409)
    assert api.post(f"{doc_url}/sign").status_code == 409
    assert api.post(f"{doc_url}/approve").json()["status"] == "APPROVED"
    assert api.get(url).json()["lifecycle"] == "Ready for signing"

    form = {
        "signed_on": business_today().isoformat(),
        "signers": '[{"party": "TENANT", "name": "Asha Rao", "designation": "Director"},'
        ' {"party": "COMPANY", "name": "Kiran Rao"}]',
    }
    for bad_file, why in (
        (b"PK\x03\x04 not a pdf", "PDF"),
        (b"%PDF" + b"x" * (20 * 1024 * 1024), "20 MB"),
    ):
        refused = api.post(
            f"{doc_url}/signed-copy",
            data=form,
            files={"file": ("s.pdf", bad_file, "application/pdf")},
        )
        assert refused.status_code == 400 and why in refused.json()["detail"], refused.text
    future = {**form, "signed_on": (business_today() + timedelta(days=2)).isoformat()}
    assert (
        api.post(
            f"{doc_url}/signed-copy", data=future, files={"file": ("s.pdf", SIGNED_PDF)}
        ).status_code
        == 400
    )
    assert (
        api.post(
            f"{doc_url}/signed-copy",
            data={**form, "signers": "[]"},
            files={"file": ("s.pdf", SIGNED_PDF)},
        ).status_code
        == 400
    )

    holds_before = holds(db_session)
    copy = api.post(
        f"{doc_url}/signed-copy",
        data={**form, "notes": "Signed at the Hyderabad office"},
        files={"file": ("acme-signed.pdf", SIGNED_PDF, "application/pdf")},
    )
    assert copy.status_code == 201, copy.text
    signed = copy.json()
    assert (signed["file_name"], signed["size_bytes"], signed["source"]) == (
        "acme-signed.pdf",
        len(SIGNED_PDF),
        "MANUAL_UPLOAD",
    )
    assert [s["party"] for s in signed["signers"]] == ["TENANT", "COMPANY"]
    plan = api.get(url).json()
    assert plan["documents"][0]["status"] == "SIGNED"
    assert plan["documents"][0]["signed_copy"]["id"] == signed["id"]
    assert plan["lifecycle"] == "Signed - copy uploaded"
    # Signing never activates the contract or holds anything (activation is separate).
    assert plan["contract_status"] == "PENDING"
    assert holds(db_session) == holds_before
    listed = api.get(f"{ADMIN}/contracts", params={"tenant_id": contract["tenant_id"]})
    assert listed.json()["items"][0]["completes_contract"] is False  # BR-025: not ACTIVE yet
    assert api.post(f"{ADMIN}/contracts/{contract['id']}/activate").status_code == 200
    listed = api.get(f"{ADMIN}/contracts", params={"tenant_id": contract["tenant_id"]})
    assert listed.json()["items"][0]["completes_contract"] is True  # ACTIVE + SIGNED
    assert holds(db_session) > holds_before  # the approval held the space, as before

    # A signed agreement is never regenerated, edited or silently overwritten.
    assert api.post(f"{url}/documents").status_code == 409
    assert api.put(f"{url}/fields", json={"values": {"lock_in": "x"}}).status_code == 409
    again = api.post(f"{doc_url}/signed-copy", data=form, files={"file": ("s.pdf", SIGNED_PDF)})
    assert again.status_code == 409
    replaced = api.post(
        f"{doc_url}/signed-copy/replace",
        data={**form, "reason": "Clearer scan"},
        files={"file": ("better.pdf", SIGNED_PDF + b"%clear\n", "application/pdf")},
    )
    assert replaced.status_code == 201, replaced.text
    copies = api.get(f"{doc_url}/signed-copies").json()
    assert [(c["file_name"], c["replace_reason"]) for c in copies] == [
        ("better.pdf", None),
        ("acme-signed.pdf", "Clearer scan"),
    ]
    link = api.get(f"{ADMIN}/contract-signed-copies/{replaced.json()['id']}/download").json()
    assert link["expires_in"] == 300
    path = link["url"].split("/files/local/")[1].split("?")[0].replace("%2F", "/")
    assert path.startswith(f"contracts/signed/{contract['id']}/")
    assert storage.get(path).startswith(b"%PDF-")

    # The client app never reaches signed copies.
    for path in (
        f"{V1}/client/contract-signed-copies/{signed['id']}/download",
        f"{V1}/client/contract-documents/{document['id']}/signed-copies",
        f"{CUSTOMER}/contract-signed-copies/{signed['id']}/download",
    ):
        assert priya.get(path).status_code == 404
    assert priya.get(f"{ADMIN}/contract-signed-copies/{signed['id']}/download").status_code == 403
    # Each step is in the history.
    history = api.get(
        f"{ADMIN}/activity",
        params={"entity_type": "contract_documents", "entity_id": document["id"]},
    ).json()["items"]
    assert {"signed_copy_uploaded", "signed_copy_replaced"} <= {e["action"] for e in history}


def test_signed_before_copies_were_kept_shows_no_copy(
    api: TestClient, hub: dict, storage: LocalStorage, db_session: Session
) -> None:
    from app.models.agreement import ContractDocument

    profile_for(api, hub["property"]["id"])
    company_template(api)
    contract = pending_contract(api, hub, gstin="36ABCDE1234F1Z5")
    url = f"{ADMIN}/contracts/{contract['id']}/agreement"
    api.post(f"{url}/prefill", json={"keys": ["legal_name", "gstin"]})
    api.put(f"{url}/fields", json={"values": {"lock_in": "11 months"}})
    document = api.post(f"{url}/documents").json()
    row = db_session.get(ContractDocument, document["id"])
    row.status = "SIGNED"  # as the retired sign step left it
    db_session.commit()
    plan = api.get(url).json()
    assert plan["lifecycle"] == "Signed (no copy uploaded)"
    assert plan["documents"][0]["signed_without_copy"] is True
    # Its evidence can still be added.
    sign_with_copy(api, document["id"])
    assert api.get(url).json()["lifecycle"] == "Signed - copy uploaded"


# --- contracts from accepted proposals ---------------------------------------------------------


def test_contract_from_an_accepted_proposal(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    made_lead = lead(api)
    today = business_today()
    proposal = api.post(
        f"{CRM}/leads/{made_lead['id']}/proposals",
        json={
            "property_id": hub["property"]["id"],
            "space_ids": [hub["seat1"]["id"], hub["seat2"]["id"]],
            "proposal_date": today.isoformat(),
            "start_date": (today + timedelta(days=30)).isoformat(),
            "end_date": (today + timedelta(days=394)).isoformat(),
            "monthly_rent": "24000",
            "security_deposit": "72000",
            "commercial_terms": "Lock-in 11 months.",
            "other_charges": "Electricity as metered.",
        },
    ).json()
    url = f"{ADMIN}/contracts/from-proposal/{proposal['id']}"
    assert api.post(url).status_code == 409  # a draft
    api.post(f"{CRM}/proposals/{proposal['id']}/send")
    assert api.post(url).status_code == 409  # sent, not accepted
    api.post(
        f"{CRM}/proposals/{proposal['id']}/accept",
        json={"accepted_on": today.isoformat(), "acceptance_note": "Email"},
    )
    no_tenant = api.post(url)
    assert (
        no_tenant.status_code == 409 and "Link the lead to a tenant" in no_tenant.json()["detail"]
    )
    tenant = api.post(f"{ADMIN}/tenants", json={"name": "Acme Ltd"}).json()
    api.post(f"{CRM}/leads/{made_lead['id']}/tenant", json={"tenant_id": tenant["id"]})

    counts = (
        holds(db_session),
        db_session.scalar(select(func.count()).select_from(Booking)),
        db_session.scalar(select(func.count()).select_from(Tenant)),
    )
    created = api.post(url)
    assert created.status_code == 201, created.text
    first = created.json()
    assert (first["status"], first["proposal_id"], first["tenant_id"]) == (
        "PENDING",
        proposal["id"],
        tenant["id"],
    )
    assert first["holds_inventory"] is False and first["terms"] == "Lock-in 11 months."
    group = db_session.scalars(
        select(TenantOccupancy).where(TenantOccupancy.proposal_id == proposal["id"])
    ).all()
    assert len(group) == 2 and len({c.group_id for c in group}) == 1
    assert sorted(c.monthly_rent for c in group) == [0, 24000]
    assert sum(c.security_deposit or 0 for c in group) == 72000
    assert all(c.contract_fields == {"other_charges": "Electricity as metered."} for c in group)
    assert (
        holds(db_session),
        db_session.scalar(select(func.count()).select_from(Booking)),
        db_session.scalar(select(func.count()).select_from(Tenant)),
    ) == counts
    # Once is enough.
    again = api.post(url)
    assert again.status_code == 409 and first["reference"] in again.json()["detail"]
    assert api.post(f"{ADMIN}/contracts/from-proposal/{made_lead['id']}").status_code == 404
