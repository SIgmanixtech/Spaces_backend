# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Contract agreements: two contract types, approved templates, template selection, the
customer's details, document generation and the document states. Word documents are built
here with python-docx; files go to a temporary local store, never to Supabase."""

import io
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import docx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.files import get_storage
from app.main import app
from app.models import Tenant
from app.services.files import FileService
from app.storage import LocalStorage
from tests.test_client_api import (  # noqa: F401  (fixtures)
    CUSTOMER,
    MONDAY,
    book,
    hub,
    priya,
    ravi,
)
from tests.test_resources import V1, post

LEASE_TEXT = [
    "LEASE DEED {{ contract.reference }}",
    "Between SIGMANIX and {{ customer.legal_name }} of {{ customer.address }}.",
    "Premises: {{ lease.premises }} at {{ property.address }}.",
    "Rent: {{ pricing.currency }} {{ lease.rent }} per month from {{ lease.commencement_date }}.",
]
LICENCE_TEXT = [
    "COWORKING LICENCE {{ contract.reference }}",
    "Licensee: {{ customer.legal_name }}. Workspace: {{ licence.workspace }}.",
    "Fee {{ licence.fee }} from {{ licence.commencement_date }}.",
]


def make_docx(lines: list[str], header: str | None = None) -> bytes:
    document = docx.Document()
    if header:
        document.sections[0].header.paragraphs[0].text = header
    for line in lines:
        document.add_paragraph(line)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def text_of(data: bytes) -> str:
    return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[LocalStorage]:
    local = LocalStorage(tmp_path, "http://testserver")
    app.dependency_overrides[get_storage] = lambda: local
    yield local
    app.dependency_overrides.pop(get_storage, None)


SIGNED_PDF = b"%PDF-1.4\n% a scan of the paper-signed agreement\n"


def sign_with_copy(api: TestClient, document_id: str, expected: int = 201) -> dict:
    """Record the signature the Phase 4B way: upload the signed copy (PDF)."""
    response = api.post(
        f"{V1}/admin/contract-documents/{document_id}/signed-copy",
        data={
            "signed_on": "2026-11-01",
            "signers": '[{"party": "TENANT", "name": "Asha Rao", "designation": "Director"}]',
        },
        files={"file": ("signed.pdf", SIGNED_PDF, "application/pdf")},
    )
    assert response.status_code == expected, response.text
    return response.json()


def upload(
    api: TestClient,
    data: bytes,
    contract_type: str = "SPACE_LEASE",
    *,
    property_id: str | None = None,
    fields: str = "",
    expected: int = 201,
) -> dict:
    form = {"contract_type": contract_type, "name": f"{contract_type} template"}
    if property_id:
        form["property_id"] = property_id
    if fields:
        form["required_fields"] = fields
    response = api.post(
        f"{V1}/contract-templates",
        data=form,
        files={"file": ("template.docx", data, "application/octet-stream")},
    )
    assert response.status_code == expected, response.text
    return response.json()


def approved_contract(api: TestClient, priya: TestClient, hub: dict) -> dict:  # noqa: F811
    """Priya's reservation turned into a contract request, approved by the property team."""
    mine = book(priya, hub["seat1"])
    start = (MONDAY + timedelta(days=3)).date()
    request = priya.post(
        f"{CUSTOMER}/bookings/{mine['id']}/contract", json={"start_date": str(start), "months": 3}
    ).json()
    api.patch(f"{V1}/contracts/{request['id']}", json={"monthly_rent": "12000"})
    assert api.post(f"{V1}/contracts/{request['id']}/activate").status_code == 200
    return request


def test_templates_are_drafts_until_approved_and_unknown_placeholders_block_approval(
    api: TestClient,
    hub: dict,
    storage: LocalStorage,  # noqa: F811
) -> None:
    good = upload(api, make_docx(LEASE_TEXT, header="Ref {{ contract.reference }}"))
    assert good["status"] == "DRAFT" and good["version"] == 1
    assert "customer.legal_name" in good["placeholders"]
    assert set(good["required_fields"]) >= {"legal_name", "address"}
    assert good["unknown_placeholders"] == []

    # A licence placeholder in a lease, or an invented one, cannot be approved.
    bad = upload(api, make_docx([*LEASE_TEXT, "{{ licence.fee }} {{ customer.salary }}"]))
    assert bad["unknown_placeholders"] == ["licence.fee", "customer.salary"]
    refused = api.post(f"{V1}/contract-templates/{bad['id']}/approve")
    assert refused.status_code == 409 and "licence.fee" in refused.json()["detail"]

    assert upload(api, b"not a word file", expected=400)["detail"].startswith("Upload a Word")
    assert upload(api, make_docx(LEASE_TEXT), fields="salary", expected=400)

    active = api.post(f"{V1}/contract-templates/{good['id']}/approve").json()
    assert active["status"] == "ACTIVE" and active["approved_at"]
    # A newer version retires the old one; nothing is deleted.
    newer = upload(api, make_docx(LEASE_TEXT))
    assert newer["version"] == 3  # bad was version 2
    api.post(f"{V1}/contract-templates/{newer['id']}/approve")
    states = {t["id"]: t["status"] for t in api.get(f"{V1}/contract-templates").json()}
    assert states[good["id"]] == "RETIRED" and states[newer["id"]] == "ACTIVE"
    assert api.get(f"{V1}/contract-templates/{newer['id']}/download").json()["url"]


def test_the_contract_type_is_never_guessed_and_the_right_template_is_chosen(
    api: TestClient,
    priya: TestClient,
    hub: dict,
    storage: LocalStorage,  # noqa: F811
) -> None:
    contract = approved_contract(api, priya, hub)
    agreement = api.get(f"{V1}/contracts/{contract['id']}/agreement").json()
    assert agreement["contract_type"] is None and "has not been set" in agreement["problem"]

    # Set on the space: a seat let under a coworking licence.
    api.patch(f"{V1}/spaces/{hub['seat1']['id']}", json={"contract_type": "COWORKING_LICENCE"})
    agreement = api.get(f"{V1}/contracts/{contract['id']}/agreement").json()
    assert (agreement["contract_type"], agreement["type_source"]) == ("COWORKING_LICENCE", "space")
    assert "No approved coworking space licence" in agreement["problem"]

    # A lease template does not stand in for the licence.
    lease = upload(api, make_docx(LEASE_TEXT))
    api.post(f"{V1}/contract-templates/{lease['id']}/approve")
    assert api.get(f"{V1}/contracts/{contract['id']}/agreement").json()["template"] is None

    general = upload(api, make_docx(LICENCE_TEXT), "COWORKING_LICENCE")
    api.post(f"{V1}/contract-templates/{general['id']}/approve")
    own = upload(
        api, make_docx(LICENCE_TEXT), "COWORKING_LICENCE", property_id=hub["property"]["id"]
    )
    api.post(f"{V1}/contract-templates/{own['id']}/approve")
    chosen = api.get(f"{V1}/contracts/{contract['id']}/agreement").json()["template"]
    assert chosen["id"] == own["id"]  # the property's own before the default

    # The contract says lease while its space says licence: refused, not resolved silently.
    api.patch(f"{V1}/contracts/{contract['id']}", json={"contract_type": "SPACE_LEASE"})
    clash = api.get(f"{V1}/contracts/{contract['id']}/agreement").json()
    assert clash["contract_type"] is None and "must resolve" in clash["problem"]


def test_customer_details_generation_idempotence_states_and_ownership(
    api: TestClient,
    priya: TestClient,
    ravi: TestClient,
    hub: dict,  # noqa: F811
    storage: LocalStorage,
    db_session: Session,
) -> None:
    contract = approved_contract(api, priya, hub)
    api.patch(f"{V1}/contracts/{contract['id']}", json={"contract_type": "SPACE_LEASE"})
    template = upload(api, make_docx(LEASE_TEXT))
    api.post(f"{V1}/contract-templates/{template['id']}/approve")
    url = f"{CUSTOMER}/contracts/{contract['id']}/agreement"

    plan = priya.get(url).json()
    assert {f["key"] for f in plan["fields"]} == {"legal_name", "address"}
    legal = next(f for f in plan["fields"] if f["key"] == "legal_name")
    assert legal["value"] is None and legal["suggested"] == "priya"  # from her record
    assert plan["can_generate"] is False and "Legal name" in plan["missing"]
    early = priya.post(f"{url}/documents")
    assert early.status_code == 400 and "Legal name" in early.json()["detail"]

    # Only customer fields are accepted, and they are validated.
    bad = priya.put(f"{url}/details", json={"values": {"monthly_rent": "1", "pan": "nope"}})
    assert bad.status_code == 400
    assert {e["field"] for e in bad.json()["errors"]} == {"monthly_rent", "pan"}
    saved = priya.put(
        f"{url}/details",
        json={"values": {"legal_name": "Priya Sharma", "address": "12 Lake Road, Hyderabad"}},
    )
    assert saved.status_code == 200 and saved.json()["can_generate"] is True

    first = priya.post(f"{url}/documents")
    assert first.status_code == 201, first.text
    document = first.json()
    assert (document["status"], document["template_version"]) == ("GENERATED", 1)
    # Retrying returns the same document.
    assert priya.post(f"{url}/documents").json()["id"] == document["id"]

    link = priya.get(f"{CUSTOMER}/contract-documents/{document['id']}/download").json()["url"]
    path = link.split("/files/local/")[1].split("?")[0]
    body = text_of(storage.get(path.replace("%2F", "/")))
    assert "Priya Sharma" in body and contract["reference"] in body
    assert "12,000.00" in body and "INR" in body and "Seat 1" in body
    assert "{{" not in body

    # Another customer sees nothing of it.
    assert ravi.get(url).status_code == 404
    assert ravi.get(f"{CUSTOMER}/contract-documents/{document['id']}/download").status_code == 404

    # Changing a detail makes a new document and supersedes the unsigned one.
    priya.put(f"{url}/details", json={"values": {"address": "7 Hill Street, Hyderabad"}})
    second = priya.post(f"{url}/documents").json()
    assert second["id"] != document["id"]
    statuses = {d["id"]: d["status"] for d in priya.get(url).json()["documents"]}
    assert statuses == {document["id"]: "SUPERSEDED", second["id"]: "GENERATED"}

    # States move only forward, one explicit step at a time; nothing is signed by itself.
    doc_url = f"{V1}/contract-documents/{second['id']}"
    assert api.post(f"{doc_url}/sign").status_code == 409
    for step, status in (
        ("approve", "APPROVED"),
        ("send", "SENT_FOR_SIGNATURE"),
    ):
        assert api.post(f"{doc_url}/{step}").json()["status"] == status
    # Phase 4B: the plain sign step is retired; the signature is the uploaded signed copy.
    retired = api.post(f"{doc_url}/sign")
    assert retired.status_code == 409 and "signed copy" in retired.json()["detail"]
    sign_with_copy(api, second["id"])
    assert (
        api.get(f"{V1}/contracts/{contract['id']}/agreement").json()["documents"][0]["status"]
        == "SIGNED"
    )
    # A signed agreement is never replaced or edited.
    assert priya.post(f"{url}/documents").status_code == 409
    assert priya.put(f"{url}/details", json={"values": {"legal_name": "X"}}).status_code == 409
    # Customers cannot move document states.
    assert priya.post(f"{V1}/contract-documents/{second['id']}/approve").status_code == 403

    # Clearing orphaned files never touches templates or agreements.
    removed = FileService(db_session, storage).purge_orphans(min_age_seconds=0, dry_run=True)
    assert not [p for p in removed if p.startswith("contracts/")]


def test_an_unapproved_contract_gets_no_agreement(
    api: TestClient,
    priya: TestClient,
    hub: dict,
    storage: LocalStorage,
    db_session: Session,  # noqa: F811
) -> None:
    mine = book(priya, hub["seat2"])
    start = (MONDAY + timedelta(days=3)).date()
    request = priya.post(
        f"{CUSTOMER}/bookings/{mine['id']}/contract", json={"start_date": str(start), "months": 1}
    ).json()
    api.patch(f"{V1}/contracts/{request['id']}", json={"contract_type": "COWORKING_LICENCE"})
    template = upload(api, make_docx(LICENCE_TEXT), "COWORKING_LICENCE")
    api.post(f"{V1}/contract-templates/{template['id']}/approve")
    plan = priya.get(f"{CUSTOMER}/contracts/{request['id']}/agreement").json()
    assert "must approve the contract" in plan["problem"] and plan["can_generate"] is False
    refused = priya.post(f"{CUSTOMER}/contracts/{request['id']}/agreement/documents")
    assert refused.status_code == 409
    tenant = db_session.scalar(select(Tenant).where(Tenant.email == "priya@example.com"))
    assert tenant is not None and uuid.UUID(request["id"])


def test_template_management_is_for_administrators_only(
    priya: TestClient,
    storage: LocalStorage,  # noqa: F811
) -> None:
    assert priya.get(f"{V1}/contract-templates").status_code == 403
    assert (
        priya.post(
            f"{V1}/contract-templates",
            data={"contract_type": "SPACE_LEASE", "name": "x"},
            files={"file": ("t.docx", make_docx(LEASE_TEXT), "application/octet-stream")},
        ).status_code
        == 403
    )
    assert post  # keeps the shared helper import used by other modules
