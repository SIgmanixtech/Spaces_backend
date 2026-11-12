# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Phases B2 and B3 (owner decisions of 2 Nov 2026): contract categories and their details,
Parking links, holds and their expiry, booking conversion, renewal, ending-soon, addenda,
templates by category and the main contract of a lead. Needs the migrated test database."""

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TenantOccupancy
from app.models.activity import ActivityLog
from app.services.occupancy import business_today
from tests.test_agreements import SIGNED_PDF, storage  # noqa: F401
from tests.test_client_access import agreement
from tests.test_client_api import MONDAY, hub  # noqa: F401
from tests.test_resources import V1, post

ADMIN = f"{V1}/admin"
BOTH = (
    '[{"party": "TENANT", "name": "Asha Rao", "designation": "Director", "company": "Acme"},'
    ' {"party": "COMPANY", "name": "Kiran Rao", "designation": "Authorised signatory"}]'
)


def tenant(api: TestClient, name: str = "Acme Pvt Ltd") -> dict:
    return post(api, "tenants", {"name": name, "email": "ops@acme.example"})


def contract(api: TestClient, hub: dict, owner: dict, *, space: str = "cabin", **extra) -> dict:
    start = business_today() + timedelta(days=10)
    body = {
        "tenant_id": owner["id"],
        "property_id": hub["property"]["id"],
        "space_id": hub[space]["id"] if space else None,
        "start_date": str(start),
        "end_date": str(start + timedelta(days=364)),
        "monthly_rent": 12000,
        **extra,
    }
    made = api.post(f"{ADMIN}/contracts", json=body)
    assert made.status_code == 201, made.text
    return made.json()


def act(api: TestClient, contract_id: str, action: str, body: dict | None = None, code=200):
    done = api.post(f"{ADMIN}/contracts/{contract_id}/{action}", json=body or {})
    assert done.status_code == code, done.text
    return done.json()


def test_category_details_are_needed_before_approval(api: TestClient, hub: dict) -> None:
    owner = tenant(api)
    made = contract(api, hub, owner, category="COMMERCIAL", end_date=None)
    assert made["category"] == "COMMERCIAL"
    assert made["missing_for_approval"] == ["expected headcount", "end date"]
    refused = act(api, made["id"], "activate", code=409)
    assert "needs expected headcount, end date" in refused["detail"]
    end = str(business_today() + timedelta(days=200))
    fixed = api.patch(
        f"{ADMIN}/contracts/{made['id']}",
        json={"expected_headcount": 25, "end_date": end, "lock_in_period": "6 months"},
    ).json()
    assert fixed["missing_for_approval"] == []
    assert act(api, made["id"], "activate")["status"] == "ACTIVE"
    # Ongoing is refused for Commercial (D10.5-D10.6).
    assert act(api, made["id"], "extend", {"end_date": None}, code=400)
    # Uncategorised contracts are approved as before (D8.6).
    old = contract(api, hub, tenant(api, "Old Ltd"), space="seat1", monthly_rent=None)
    assert old["category"] is None and old["missing_for_approval"] == []
    assert act(api, old["id"], "activate")["status"] == "ACTIVE"


def test_parking_links_follow_the_main_contract(api: TestClient, hub: dict) -> None:
    owner = tenant(api)
    main = contract(api, hub, owner, category="OFFICE", occupants=4)
    act(api, main["id"], "activate")
    # Only Parking links to a main contract, and only the same tenant's.
    refused = api.post(
        f"{ADMIN}/contracts",
        json={
            "tenant_id": owner["id"],
            "property_id": hub["property"]["id"],
            "space_id": hub["seat1"]["id"],
            "start_date": main["start_date"],
            "category": "SPACE",
            "main_contract_id": main["id"],
        },
    )
    assert refused.status_code == 400 and "Only a Parking" in refused.text
    other = contract(
        api,
        hub,
        tenant(api, "Other Ltd"),
        space="seat2",
        category="PARKING",
        vehicle_registration="TS09AB1234",
    )
    wrong = api.patch(f"{ADMIN}/contracts/{other['id']}", json={"main_contract_id": main["id"]})
    assert wrong.status_code == 400 and "another tenant" in wrong.text
    parking = contract(
        api,
        hub,
        owner,
        space="seat1",
        category="PARKING",
        vehicle_registration="TS09AB9999",
        main_contract_id=main["id"],
        end_date=main["end_date"],
    )
    assert parking["main_contract_reference"] == main["reference"]
    act(api, parking["id"], "activate")
    # Extending the main contract extends parking ending on the same day (LC-6).
    later = str(business_today() + timedelta(days=500))
    act(api, main["id"], "extend", {"end_date": later})
    assert api.get(f"{ADMIN}/contracts/{parking['id']}").json()["end_date"] == later
    # Cancelling the main contract (not started yet) cancels its parking too (LC-5).
    act(api, main["id"], "cancel", {"reason": "Lost"})
    assert api.get(f"{ADMIN}/contracts/{parking['id']}").json()["status"] == "CANCELLED"


def test_holds_keep_the_space_and_expire_automatically(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    first = contract(api, hub, tenant(api))
    assert first["holds_inventory"] is False and first["hold_until"] is None
    held = act(api, first["id"], "hold", {})
    assert held["holds_inventory"] is True
    assert held["hold_until"] == str(business_today() + timedelta(days=7))
    # Someone else cannot take the space while it is held.
    rival = contract(api, hub, tenant(api, "Rival Ltd"))
    assert act(api, rival["id"], "activate", code=409)
    # The hold runs out: it is released automatically, recorded, and the rival gets it.
    row = db_session.get(TenantOccupancy, first["id"])
    row.hold_until = business_today() - timedelta(days=1)
    db_session.commit()
    assert act(api, rival["id"], "activate")["status"] == "ACTIVE"
    again = api.get(f"{ADMIN}/contracts/{first['id']}").json()
    assert again["status"] == "PENDING" and again["holds_inventory"] is False
    db_session.expire_all()
    logged = db_session.scalars(
        select(ActivityLog.message).where(
            ActivityLog.entity_id == row.id, ActivityLog.action == "hold_expired"
        )
    ).all()
    assert len(logged) == 1 and "released automatically" in logged[0]
    # Approval re-checks availability: the space is taken now.
    assert act(api, first["id"], "activate", code=409)


def test_a_monthly_booking_becomes_a_held_contract(api: TestClient, hub: dict) -> None:
    owner = tenant(api)
    start = MONDAY + timedelta(days=30)

    def booking(days: int, space: str) -> dict:
        return post(
            api,
            "bookings",
            {
                "property_id": hub["property"]["id"],
                "space_id": hub[space]["id"],
                "tenant_id": owner["id"],
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(days=days)).isoformat(),
            },
        )

    short = booking(5, "seat2")
    refused = api.post(f"{ADMIN}/contracts/from-booking/{short['id']}", json={})
    assert refused.status_code == 409 and "stay bookings" in refused.text
    long = booking(40, "seat1")
    made = api.post(
        f"{ADMIN}/contracts/from-booking/{long['id']}",
        json={"category": "SPACE", "monthly_rent": 9000},
    )
    assert made.status_code == 201, made.text
    converted = made.json()
    assert converted["booking_id"] == long["id"] and converted["status"] == "PENDING"
    assert converted["holds_inventory"] is True and converted["hold_until"]
    assert float(converted["monthly_rent"]) == 9000
    booked = api.get(f"{V1}/bookings/{long['id']}").json()
    assert booked["status"] == "CONVERTED" and booked["contract_id"] == converted["id"]
    # A booking is converted once.
    assert api.post(f"{ADMIN}/contracts/from-booking/{long['id']}", json={}).status_code == 409


def test_renewal_is_a_new_linked_contract_from_the_next_day(api: TestClient, hub: dict) -> None:
    made = contract(api, hub, tenant(api), category="SPACE")
    act(api, made["id"], "activate")
    renewal = api.post(
        f"{ADMIN}/contracts/{made['id']}/renew", json={"monthly_rent": "13000"}
    ).json()
    end = datetime.fromisoformat(made["end_date"]).date()
    assert renewal["start_date"] == str(end + timedelta(days=1))
    assert (
        renewal["status"] == "PENDING" and renewal["renews_contract_reference"] == made["reference"]
    )
    assert float(renewal["monthly_rent"]) == 13000 and renewal["category"] == "SPACE"
    assert (
        api.get(f"{ADMIN}/contracts/{made['id']}").json()["renewed_by_reference"]
        == (renewal["reference"])
    )
    again = api.post(f"{ADMIN}/contracts/{made['id']}/renew", json={})
    assert again.status_code == 409 and "already renewed" in again.text
    # Approved, the renewal holds from the day after: no clash with the old contract.
    assert act(api, renewal["id"], "activate")["status"] == "ACTIVE"


def test_contracts_ending_within_60_days(api: TestClient, hub: dict, db_session: Session) -> None:
    owner = tenant(api)
    soon = contract(api, hub, owner, space="seat1")
    later = contract(api, hub, owner, space="seat2")
    for made, days in ((soon, 30), (later, 90)):
        act(api, made["id"], "activate")
        row = db_session.get(TenantOccupancy, made["id"])
        row.start_date = business_today() - timedelta(days=5)
        row.end_date = business_today() + timedelta(days=days)
    db_session.commit()
    listed = [c["reference"] for c in api.get(f"{ADMIN}/contracts/ending-soon").json()]
    assert soon["reference"] in listed and later["reference"] not in listed
    wider = api.get(f"{ADMIN}/contracts/ending-soon", params={"days": 120}).json()
    assert [c["reference"] for c in wider] == [soon["reference"], later["reference"]]


def test_addenda_apply_only_once_signed_by_both(
    api: TestClient, hub: dict, storage: object
) -> None:
    made = contract(api, hub, tenant(api))
    api.patch(f"{ADMIN}/contracts/{made['id']}", json={"contract_type": "SPACE_LEASE"})
    act(api, made["id"], "activate")
    agreement(api, api.get(f"{ADMIN}/contracts/{made['id']}").json(), up_to="done")
    # The agreement is signed: terms change only with an addendum.
    direct = api.patch(f"{ADMIN}/contracts/{made['id']}", json={"monthly_rent": 15000})
    assert direct.status_code == 409 and "addendum" in direct.text
    assert api.patch(f"{ADMIN}/contracts/{made['id']}", json={"notes": "ok"}).status_code == 200
    drafted = api.post(
        f"{ADMIN}/contracts/{made['id']}/addenda",
        json={
            "effective_date": str(business_today() + timedelta(days=40)),
            "summary": "Rent revised",
            "changes": {"monthly_rent": "15000", "notice_period": "3 months"},
        },
    )
    assert drafted.status_code == 201, drafted.text
    addendum = drafted.json()
    assert addendum["reference"].startswith("AD-") and addendum["status"] == "DRAFT"
    # A draft changes nothing (LC-4).
    before = api.get(f"{ADMIN}/contracts/{made['id']}").json()
    assert float(before["monthly_rent"]) == 12000
    dates = api.post(
        f"{ADMIN}/contracts/{made['id']}/addenda",
        json={
            "effective_date": str(business_today()),
            "summary": "x",
            "changes": {"end_date": "2030-01-01"},
        },
    )
    assert dates.status_code == 400 and "extension or renewal" in dates.text

    def sign(signers: str, code: int) -> dict:
        response = api.post(
            f"{ADMIN}/addenda/{addendum['id']}/signed-copy",
            data={"signed_on": str(business_today()), "signers": signers},
            files={"file": ("addendum.pdf", SIGNED_PDF, "application/pdf")},
        )
        assert response.status_code == code, response.text
        return response.json()

    one = '[{"party": "TENANT", "name": "Asha Rao"}]'
    assert "Both parties" in sign(one, 400)["detail"]
    signed = sign(BOTH, 200)
    assert signed["status"] == "SIGNED" and float(signed["previous"]["monthly_rent"]) == 12000
    now = api.get(f"{ADMIN}/contracts/{made['id']}").json()
    assert (float(now["monthly_rent"]), now["notice_period"]) == (15000, "3 months")
    assert api.get(f"{ADMIN}/addenda/{addendum['id']}/signed-copy/download").status_code == 200
    assert [a["status"] for a in api.get(f"{ADMIN}/contracts/{made['id']}/addenda").json()] == [
        "SIGNED"
    ]


def test_customer_request_holds_seven_days_and_can_ask_for_more(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    from app.services.contracts import ContractService

    owner = tenant(api)
    service = ContractService(db_session, None)
    row = service.request(
        {
            "tenant_id": uuid.UUID(owner["id"]),
            "property_id": uuid.UUID(hub["property"]["id"]),
            "space_id": uuid.UUID(hub["cabin"]["id"]),
            "start_date": business_today() + timedelta(days=20),
            "end_date": business_today() + timedelta(days=200),
        }
    )
    assert row.hold_until == business_today() + timedelta(days=7)
    service.ask_hold_extension(row, business_today() + timedelta(days=12), "Board approval")
    read = api.get(f"{ADMIN}/contracts/{row.id}").json()
    assert read["hold_extension_requested_until"] == str(business_today() + timedelta(days=12))
    approved = act(api, str(row.id), "hold", {"hold_until": read["hold_extension_requested_until"]})
    assert approved["hold_until"] == read["hold_extension_requested_until"]
    assert approved["hold_extension_requested_until"] is None
