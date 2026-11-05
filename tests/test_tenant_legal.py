# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Tenant legal and signatory details (Phase 4A).

The schema tests need no database; the API tests need the migrated test database (like
every API test here)."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models import Tenant
from app.schemas.tenant import TenantCreate, TenantUpdate
from tests.test_client_api import priya  # noqa: F401
from tests.test_resources import V1

TENANTS = f"{V1}/admin/tenants"
LEGAL = {
    "registered_address": "Plot 12, Hitech City, Hyderabad 500081",
    "signatory_name": "Asha Rao",
    "signatory_designation": "Director",
    "signatory_address": "Flat 4, Banjara Hills, Hyderabad",
    "gstin": "36ABCDE1234F1Z5",
    "pan": "ABCDE1234F",
    "cin": "U72200TG2015PTC123456",
}


# --- formats (no database) -----------------------------------------------------------------


def test_legal_details_are_optional_trimmed_and_uppercased() -> None:
    empty = TenantCreate(name="Acme")
    assert all(getattr(empty, key) is None for key in LEGAL)
    tidy = TenantCreate(
        name="Acme",
        gstin=" 36abcde1234f1z5 ",
        pan="abcde1234f",
        cin=" u72200tg2015ptc123456",
        signatory_name="  ",
        registered_address=" Plot 12 ",
    )
    assert (tidy.gstin, tidy.pan, tidy.cin) == (
        "36ABCDE1234F1Z5",
        "ABCDE1234F",
        "U72200TG2015PTC123456",
    )
    # Blank text is stored as empty, not as spaces.
    assert tidy.signatory_name is None
    assert tidy.registered_address == "Plot 12"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gstin", "36ABCDE1234F1Z"),  # 14 characters
        ("gstin", "36ABCDE1234F1X5"),  # the 14th character must be Z
        ("pan", "ABCD1234F"),
        ("pan", "ABCDE12345"),
        ("cin", "U72200TG2015PTC12345"),  # 20 characters
        ("cin", "X72200TG2015PTC123456"),  # must start with L or U
        ("cin", "U7220TTG2015PTC123456"),
    ],
)
def test_wrong_formats_are_refused(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        TenantCreate(name="Acme", **{field: value})


def test_a_listed_company_cin_is_accepted() -> None:
    assert TenantCreate(name="Acme", cin="L17110MH1973PLC019786").cin == "L17110MH1973PLC019786"


def test_there_is_no_aadhaar_field() -> None:
    for schema in (TenantCreate, TenantUpdate):
        assert not any("aadhaar" in name for name in schema.model_fields)
    assert not any("aadhaar" in column.key for column in Tenant.__table__.columns)
    with pytest.raises(ValidationError):  # unknown fields are refused, not stored
        TenantCreate(name="Acme", aadhaar="1234 5678 9012")  # type: ignore[call-arg]


def test_an_update_without_legal_fields_leaves_them_alone() -> None:
    # PATCH semantics: the existing Properties > Tenants form sends no legal fields.
    assert TenantUpdate(name="Acme").model_dump(exclude_unset=True) == {"name": "Acme"}


# --- API (test database) -------------------------------------------------------------------


def test_legal_details_save_load_and_history_names_fields_only(api: TestClient) -> None:
    made = api.post(TENANTS, json={"name": "Acme Ltd", **LEGAL})
    assert made.status_code == 201, made.text
    tenant = made.json()
    assert {key: tenant[key] for key in LEGAL} == LEGAL
    assert api.get(f"{TENANTS}/{tenant['id']}").json()["gstin"] == LEGAL["gstin"]

    changed = api.patch(
        f"{TENANTS}/{tenant['id']}", json={"pan": "pqrst6789k", "signatory_name": "Ravi Rao"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["pan"] == "PQRST6789K"
    history = api.get(
        f"{V1}/admin/activity", params={"entity_type": "tenants", "entity_id": tenant["id"]}
    ).json()["items"]
    text = " ".join(entry["message"] for entry in history)
    assert "PAN" in text and "signatory name" in text
    for value in (*LEGAL.values(), "PQRST6789K", "Ravi Rao"):
        assert value not in text

    # The Properties > Tenants form sends only its own fields: legal details stay.
    kept = api.patch(f"{TENANTS}/{tenant['id']}", json={"name": "Acme Limited", "notes": "x"})
    assert kept.json()["gstin"] == LEGAL["gstin"]
    assert kept.json()["cin"] == LEGAL["cin"]
    # Clearing one is explicit.
    cleared = api.patch(f"{TENANTS}/{tenant['id']}", json={"cin": None})
    assert cleared.json()["cin"] is None


def test_tenants_without_legal_details_stay_valid(api: TestClient, db_session: Session) -> None:
    db_session.add(Tenant(name="Old Tenant"))
    db_session.commit()
    listed = api.get(TENANTS, params={"search": "Old Tenant"}).json()["items"]
    assert len(listed) == 1
    assert all(listed[0][key] is None for key in LEGAL)
    assert api.post(TENANTS, json={"name": "Plain Tenant"}).status_code == 201
    bad = api.post(TENANTS, json={"name": "Bad", "gstin": "12345"})
    assert bad.status_code == 422


def test_the_client_app_never_shows_legal_details(priya: TestClient, db_session: Session) -> None:
    tenant = db_session.scalar(select(Tenant).where(Tenant.email == "priya@example.com"))
    for key, value in LEGAL.items():
        setattr(tenant, key, value)
    db_session.commit()
    # Every client GET without path parameters.
    paths = [
        path
        for path, ops in app.openapi()["paths"].items()
        if path.startswith("/client/api/v1/") and "get" in ops and "{" not in path
    ]
    assert "/client/api/v1/me" in paths
    seen = 0
    for path in paths:
        response = priya.get(path)
        if response.status_code != 200:
            continue
        seen += 1
        for value in LEGAL.values():
            assert value not in response.text, path
    assert seen > 0
