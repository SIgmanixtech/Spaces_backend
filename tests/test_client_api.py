"""The client app's backend: public catalogue, customer accounts, ownership and booking.

Sigma Hub > Tower A > Floor 5 > Portions 5A and 5B. 5A has a cabin; 5B has an open bay
with two seats. Prices are test data entered here, never defaults of the application.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.models import AppUser, Booking, Tenant
from app.models.enums import UserRole
from app.services.auth import SignUp, SupabaseSession, get_auth_gateway
from tests.conftest import make_token
from tests.test_properties import create_property
from tests.test_resources import V1, post

PUBLIC = f"{V1}/public"
CUSTOMER = f"{V1}/customer"
# Next Monday, 09:00 India time, well in the future.
_today = datetime.now(UTC).date()
MONDAY = datetime.combine(
    _today + timedelta(days=(7 - _today.weekday()) % 7 + 14), datetime.min.time()
).replace(hour=9, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def iso(moment: datetime) -> str:
    return moment.isoformat()


@pytest.fixture
def hub(api: TestClient) -> dict[str, dict]:
    prop = create_property(api, name="Sigma Hub", description="Offices and coworking.")
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Tower A"})
    floor = post(
        api, "floors", {"building_id": building["id"], "name": "Floor 5", "level_number": 5}
    )

    def space(name: str, space_type: str, **extra: object) -> dict:
        return post(
            api,
            "spaces",
            {"property_id": prop["id"], "name": name, "space_type": space_type, **extra},
        )

    root = space("Floor 5", "FLOOR", floor_id=floor["id"], is_rentable=False)
    a5 = space("Portion 5A", "PORTION", parent_space_id=root["id"], area_sqft=1650)
    b5 = space("Portion 5B", "PORTION", parent_space_id=root["id"], area_sqft=1650)
    cabin = space(
        "Cabin 1",
        "CABIN",
        parent_space_id=a5["id"],
        area_sqft=200,
        capacity=4,
        x=3,
        y=5,
        width=7,
        height=4,
    )
    bay = space("Open Bay", "COWORKING", parent_space_id=b5["id"], capacity=20)
    seat1 = space("Seat 1", "SEAT", parent_space_id=bay["id"], capacity=1)
    seat2 = space("Seat 2", "SEAT", parent_space_id=bay["id"], capacity=1)
    for body in (
        {"rate_basis": "PER_SQFT", "billing_period": "MONTHLY", "amount": "70"},
        {
            "rate_basis": "PER_SEAT",
            "billing_period": "HOURLY",
            "amount": "100",
            "applies_to": "SEAT",
        },
        {
            "rate_basis": "PER_SEAT",
            "billing_period": "DAILY",
            "amount": "500",
            "applies_to": "SEAT",
        },
    ):
        post(
            api,
            "pricing-rules",
            {"property_id": prop["id"], "effective_from": "2026-01-01", "status": "ACTIVE", **body},
        )
    api.patch(f"{V1}/properties/{prop['id']}", json={"is_published": True})
    return {
        "property": prop,
        "building": building,
        "floor": floor,
        "5A": a5,
        "5B": b5,
        "cabin": cabin,
        "bay": bay,
        "seat1": seat1,
        "seat2": seat2,
    }


def customer_client(db_session: Session, key: ec.EllipticCurvePrivateKey, email: str):
    user = AppUser(
        id=uuid.uuid4(), email=email, full_name=email.split("@")[0], role=UserRole.CUSTOMER
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(Tenant(name=user.full_name, email=email, user_id=user.id))
    db_session.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {make_token(key, user.id)}"})


@pytest.fixture
def priya(verifier: None, db_session: Session, signing_key) -> Iterator[TestClient]:
    with customer_client(db_session, signing_key, "priya@example.com") as client:
        yield client


@pytest.fixture
def ravi(verifier: None, db_session: Session, signing_key) -> Iterator[TestClient]:
    with customer_client(db_session, signing_key, "ravi@example.com") as client:
        yield client


def floor_states(client: TestClient, hub: dict, start: datetime, hours: int = 1) -> dict:
    response = client.get(
        f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}/availability",
        params={"start_at": iso(start), "end_at": iso(start + timedelta(hours=hours))},
    )
    assert response.status_code == 200, response.text
    names = {
        s["id"]: s["name"]
        for s in client.get(
            f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}"
        ).json()["spaces"]
    }
    return {names[s["space_id"]]: s["status"] for s in response.json()["spaces"]}


def book(
    client: TestClient,
    space: dict,
    start: datetime = MONDAY,
    hours: int = 1,
    period: str = "HOURLY",
    expected: int = 201,
) -> dict:
    response = client.post(
        f"{CUSTOMER}/bookings",
        json={
            "space_id": space["id"],
            "billing_period": period,
            "start_at": iso(start),
            "end_at": iso(start + timedelta(hours=hours)),
        },
    )
    assert response.status_code == expected, response.text
    return response.json()


# --- public catalogue -------------------------------------------------------------------


def test_only_published_properties_are_listed_without_sign_in(api: TestClient, hub: dict) -> None:
    hidden = create_property(api, name="Draft Tower", is_published=False)
    anonymous = TestClient(app)

    listed = anonymous.get(f"{PUBLIC}/properties").json()
    assert [p["name"] for p in listed["items"]] == ["Sigma Hub"]
    card = listed["items"][0]
    assert card["summary"] == "Offices and coworking."
    assert set(card["space_types"]) == {"PORTION", "CABIN", "COWORKING", "SEAT"}
    assert {(p["billing_period"], p["amount"]) for p in card["starting_prices"]} == {
        ("HOURLY", 100.0),
        ("DAILY", 500.0),
        ("MONTHLY", 14000.0),
    }
    assert anonymous.get(f"{PUBLIC}/properties/{hidden['id']}").status_code == 404

    api.patch(f"{V1}/properties/{hub['property']['id']}", json={"status": "INACTIVE"})
    assert anonymous.get(f"{PUBLIC}/properties").json()["total"] == 0


def test_filters_use_backend_data(hub: dict) -> None:
    anonymous = TestClient(app)
    count = lambda **params: anonymous.get(f"{PUBLIC}/properties", params=params).json()["total"]  # noqa: E731
    assert count(space_type="SEAT") == 1
    assert count(space_type="TRAINING_ROOM") == 0
    assert count(min_capacity=20) == 1 and count(min_capacity=21) == 0
    assert count(billing_period="DAILY", max_price=500) == 1
    assert count(billing_period="DAILY", max_price=499) == 0
    assert count(billing_period="WEEKLY") == 0
    assert count(city="hyder") == 1 and count(city="Pune") == 0


def test_detail_and_floor_plan_show_hierarchy_geometry_and_prices(hub: dict) -> None:
    anonymous = TestClient(app)
    detail = anonymous.get(f"{PUBLIC}/properties/{hub['property']['id']}").json()
    assert [(b["name"], [f["name"] for f in b["floors"]]) for b in detail["buildings"]] == [
        ("Tower A", ["Floor 5"])
    ]
    plan = anonymous.get(
        f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}"
    ).json()
    spaces = {s["name"]: s for s in plan["spaces"]}
    assert spaces["Cabin 1"]["parent_space_id"] == hub["5A"]["id"]
    assert (spaces["Cabin 1"]["x"], spaces["Cabin 1"]["width"]) == (3, 7)
    assert spaces["Floor 5"]["bookable"] is False and spaces["Seat 1"]["bookable"] is True
    seat_prices = {p["billing_period"]: p for p in plan["prices"][hub["seat1"]["id"]]}
    assert seat_prices["DAILY"]["estimate"]["rent"] == 500.0
    assert "created_by" not in seat_prices["DAILY"]["rule"]


def test_quote_needs_whole_billing_periods(hub: dict) -> None:
    anonymous = TestClient(app)
    body = {
        "space_id": hub["seat1"]["id"],
        "billing_period": "HOURLY",
        "start_at": iso(MONDAY),
        "end_at": iso(MONDAY + timedelta(hours=3)),
    }
    quote = anonymous.post(f"{PUBLIC}/quotes", json=body).json()
    assert (quote["quantity"], quote["estimate"]["rent"]) == (3.0, 300.0)
    assert quote["availability"]["status"] == "AVAILABLE"
    odd = anonymous.post(
        f"{PUBLIC}/quotes", json={**body, "end_at": iso(MONDAY + timedelta(minutes=90))}
    )
    assert odd.status_code == 400 and "whole hours" in odd.json()["detail"]
    weekly = anonymous.post(
        f"{PUBLIC}/quotes",
        json={**body, "billing_period": "WEEKLY", "end_at": iso(MONDAY + timedelta(days=7))},
    )
    assert weekly.status_code == 404  # no weekly price: nothing is made up


# --- hierarchy, through the customer's eyes --------------------------------------------


def test_a_portion_lease_blocks_what_is_inside_it_and_not_the_other_portion(
    api: TestClient, hub: dict
) -> None:
    tenant = post(api, "tenants", {"name": "Acme"})
    post(
        api,
        "contracts",
        {
            "tenant_id": tenant["id"],
            "property_id": hub["property"]["id"],
            "space_id": hub["5A"]["id"],
            "start_date": str(date.today()),
            "activate": True,
        },
    )
    states = floor_states(TestClient(app), hub, MONDAY)
    assert states["Portion 5A"] == "OCCUPIED"
    assert states["Cabin 1"] == "OCCUPIED"
    assert states["Portion 5B"] == "AVAILABLE" and states["Seat 1"] == "AVAILABLE"
    assert states["Floor 5"] == "UNAVAILABLE"  # not rentable


def test_a_seat_booking_holds_only_that_seat(priya: TestClient, hub: dict) -> None:
    booking = book(priya, hub["seat1"])
    assert booking["status"] == "PENDING"
    states = floor_states(TestClient(app), hub, MONDAY)
    assert states["Seat 1"] == "RESERVED"
    assert states["Seat 2"] == "AVAILABLE"
    assert states["Open Bay"] == "UNAVAILABLE" and states["Portion 5B"] == "UNAVAILABLE"
    assert states["Portion 5A"] == "AVAILABLE"
    # The bay cannot then be taken whole, in either order.
    book(priya, hub["bay"], period="MONTHLY", hours=24 * 30, expected=409)


# --- customer accounts -------------------------------------------------------------------


class FakeGateway:
    def __init__(self, key: ec.EllipticCurvePrivateKey, user_id: uuid.UUID, existing=False):
        self.key, self.user_id, self.existing, self.recovered = key, user_id, existing, []

    def sign_up(self, email: str, password: str, full_name: str) -> SignUp:
        if self.existing:
            return SignUp(user_id=None, session=None, existing=True)
        token = make_token(self.key, self.user_id, email=email)
        return SignUp(str(self.user_id), SupabaseSession(token, "refresh", 3600), False)

    def recover(self, email: str) -> None:
        self.recovered.append(email)


def test_registration_creates_a_customer_and_their_tenant_only(
    verifier: None, db_session: Session, signing_key
) -> None:
    user_id = uuid.uuid4()
    gateway = FakeGateway(signing_key, user_id)
    app.dependency_overrides[get_auth_gateway] = lambda: gateway
    try:
        client = TestClient(app)
        body = {
            "email": "New@Example.com",
            "password": "long-enough",
            "full_name": "Meera",
            "phone": "+91 90000 00000",
        }
        created = client.post(f"{CUSTOMER}/register", json=body)
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "SIGNED_IN"
        assert created.json()["session"]["user"]["role"] == "CUSTOMER"
        tenant = db_session.scalar(select(Tenant).where(Tenant.user_id == user_id))
        assert (tenant.name, tenant.email, tenant.phone) == (
            "Meera",
            "new@example.com",
            "+91 90000 00000",
        )
        again = client.post(f"{CUSTOMER}/register", json=body)
        assert again.status_code == 409
        gateway.existing = True
        assert (
            client.post(f"{CUSTOMER}/register", json={**body, "email": "x@example.com"}).status_code
            == 409
        )
        assert (
            client.post(f"{CUSTOMER}/register", json={**body, "role": "ADMIN"}).status_code == 422
        )
        assert (
            client.post(
                f"{CUSTOMER}/password-recovery", json={"email": "a@example.com"}
            ).status_code
            == 202
        )
    finally:
        app.dependency_overrides.pop(get_auth_gateway, None)


def test_admin_and_customer_permissions_do_not_overlap(api: TestClient, priya: TestClient) -> None:
    assert priya.get(f"{V1}/properties").status_code == 403
    assert priya.get(f"{V1}/bookings").status_code == 403
    assert api.get(f"{CUSTOMER}/bookings").status_code == 403
    assert TestClient(app).get(f"{CUSTOMER}/bookings").status_code == 401


def test_customers_only_ever_see_their_own_bookings(
    priya: TestClient, ravi: TestClient, hub: dict, db_session: Session
) -> None:
    mine = book(priya, hub["seat1"], hours=3)
    assert mine["quoted_total"] == 300.0 and mine["billing_period"] == "HOURLY"
    stored = db_session.get(Booking, uuid.UUID(mine["id"]))
    assert (stored.channel, stored.customer_email) == ("CUSTOMER", "priya@example.com")

    assert [b["id"] for b in priya.get(f"{CUSTOMER}/bookings").json()["items"]] == [mine["id"]]
    assert ravi.get(f"{CUSTOMER}/bookings").json()["total"] == 0
    assert ravi.get(f"{CUSTOMER}/bookings/{mine['id']}").status_code == 404
    assert ravi.post(f"{CUSTOMER}/bookings/{mine['id']}/cancel").status_code == 404

    # Ravi wants the same seat: told it is reserved, never whose or which reference.
    refused = book(ravi, hub["seat1"], expected=409)
    assert refused["detail"].startswith("Reserved")
    assert mine["reference"] not in str(refused) and "priya" not in str(refused).lower()
    assert "conflicts" not in refused
    book(ravi, hub["seat2"])  # the next seat is free

    cancelled = priya.post(f"{CUSTOMER}/bookings/{mine['id']}/cancel", json={"reason": "Plans"})
    assert cancelled.json()["status"] == "CANCELLED" and cancelled.json()["can_cancel"] is False
    assert priya.post(f"{CUSTOMER}/bookings/{mine['id']}/cancel").status_code == 409


def test_a_customer_cannot_choose_whom_a_booking_is_for(priya: TestClient, hub: dict) -> None:
    sneaky = priya.post(
        f"{CUSTOMER}/bookings",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "HOURLY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=1)),
            "tenant_id": str(uuid.uuid4()),
        },
    )
    assert sneaky.status_code == 422


def test_unpublished_or_unpriced_spaces_cannot_be_booked(
    api: TestClient, priya: TestClient, hub: dict
) -> None:
    refused = book(priya, hub["cabin"], period="DAILY", hours=24, expected=409)
    assert "no daily price" in refused["detail"]
    api.patch(f"{V1}/properties/{hub['property']['id']}", json={"is_published": False})
    book(priya, hub["seat1"], expected=404)


def test_customers_see_only_their_own_contracts(
    api: TestClient, priya: TestClient, ravi: TestClient, hub: dict, db_session: Session
) -> None:
    tenant = db_session.scalar(select(Tenant).where(Tenant.email == "priya@example.com"))
    contract = post(
        api,
        "contracts",
        {
            "tenant_id": str(tenant.id),
            "property_id": hub["property"]["id"],
            "space_id": hub["5A"]["id"],
            "start_date": str(date.today()),
            "monthly_rent": "115500",
            "activate": True,
        },
    )
    listed = priya.get(f"{CUSTOMER}/contracts").json()
    assert [(c["reference"], c["scope"], c["status"]) for c in listed] == [
        (contract["reference"], "Portion: Portion 5A", "ACTIVE")
    ]
    assert ravi.get(f"{CUSTOMER}/contracts").json() == []
    assert ravi.get(f"{CUSTOMER}/contracts/{contract['id']}").status_code == 404
    # Inside her own lease Priya may still book the cabin; another customer may not.
    api.post(
        f"{V1}/pricing-rules",
        json={
            "property_id": hub["property"]["id"],
            "space_id": hub["cabin"]["id"],
            "rate_basis": "PER_CABIN",
            "billing_period": "DAILY",
            "amount": "2000",
            "effective_from": "2026-01-01",
            "status": "ACTIVE",
        },
    )
    book(ravi, hub["cabin"], period="DAILY", hours=24, expected=409)
    book(priya, hub["cabin"], period="DAILY", hours=24)


def test_profile_is_the_signed_in_customers(priya: TestClient) -> None:
    assert priya.get(f"{CUSTOMER}/me").json()["email"] == "priya@example.com"
    updated = priya.patch(f"{CUSTOMER}/me", json={"phone": "123", "company_name": "Nair & Co"})
    assert (updated.json()["phone"], updated.json()["company_name"]) == ("123", "Nair & Co")
    assert priya.patch(f"{CUSTOMER}/me", json={"email": "x@example.com"}).status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "bookings", {"billing_period": "HOURLY"}),
        ("post", "bookings/00000000-0000-0000-0000-000000000000/reschedule", {}),
        ("post", "bookings/00000000-0000-0000-0000-000000000000/cancel", {}),
        ("get", "bookings", None),
        ("get", "contracts", None),
        ("get", "me", None),
    ],
)
def test_booking_needs_a_signed_in_customer(
    api: TestClient, hub: dict, method: str, path: str, body: dict | None
) -> None:
    """No booking (or any customer data) without signing in; an admin token is not a
    customer either. Checked before the request body is even looked at."""
    url = f"{CUSTOMER}/{path}"
    if body is not None and path == "bookings":
        body = {
            **body,
            "space_id": hub["seat1"]["id"],
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=1)),
        }
    anonymous = getattr(TestClient(app), method)(
        url, **({"json": body} if body is not None else {})
    )
    assert anonymous.status_code == 401
    as_admin = getattr(api, method)(url, **({"json": body} if body is not None else {}))
    assert as_admin.status_code == 403
    # Nothing was booked by either attempt.
    assert api.get(f"{V1}/bookings").json()["total"] == 0


# --- the property team manages customer bookings -----------------------------------------


def test_admin_sees_and_manages_a_customer_booking(
    api: TestClient, priya: TestClient, hub: dict
) -> None:
    mine = book(priya, hub["seat1"], hours=2)

    # The admin list shows it with who booked, where, from which app and at what price.
    listed = api.get(f"{V1}/bookings", params={"status": "PENDING"}).json()["items"]
    row = next(b for b in listed if b["id"] == mine["id"])
    assert row["reference"] == mine["reference"]
    assert (row["property_name"], row["space_name"]) == ("Sigma Hub", "Seat 1")
    assert (row["tenant_name"], row["customer_email"]) == ("priya", "priya@example.com")
    assert (row["channel"], row["billing_period"]) == ("CUSTOMER", "HOURLY")
    assert (row["quoted_total"], row["quoted_currency"]) == (200.0, mine["quoted_currency"])
    assert row["quote"] == mine["quote"]
    assert api.get(f"{V1}/bookings/{mine['id']}").json()["channel"] == "CUSTOMER"

    def seen_by_customer() -> dict:
        return priya.get(f"{CUSTOMER}/bookings/{mine['id']}").json()

    # Confirmed by the property team: the customer sees it confirmed.
    assert api.post(f"{V1}/bookings/{mine['id']}/confirm").json()["status"] == "CONFIRMED"
    assert seen_by_customer()["status"] == "CONFIRMED"
    # It has not started, so it cannot be completed yet.
    assert api.post(f"{V1}/bookings/{mine['id']}/complete").status_code == 409

    # Moved by the property team to three hours a day later: re-priced for the customer.
    start = MONDAY + timedelta(days=1)
    moved = api.patch(
        f"{V1}/bookings/{mine['id']}",
        json={"start_at": iso(start), "end_at": iso(start + timedelta(hours=3))},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["quoted_total"] == 300.0
    after = seen_by_customer()
    assert (after["start_at"], after["quoted_total"]) == (moved.json()["start_at"], 300.0)
    assert after["status"] == "CONFIRMED"

    # A time the customer's period cannot price clears the estimate instead of keeping it.
    odd = api.patch(
        f"{V1}/bookings/{mine['id']}",
        json={"start_at": iso(start), "end_at": iso(start + timedelta(minutes=90))},
    )
    assert odd.status_code == 200, odd.text
    assert odd.json()["quoted_total"] is None and seen_by_customer()["quoted_total"] is None

    # Cancelled by the property team, with a reason: the customer sees that too.
    cancelled = api.post(f"{V1}/bookings/{mine['id']}/cancel", json={"reason": "Maintenance"})
    assert cancelled.json()["status"] == "CANCELLED"
    final = seen_by_customer()
    assert final["status"] == "CANCELLED" and final["can_cancel"] is False
    # The seat is free again for anyone.
    assert floor_states(priya, hub, start, hours=3)["Seat 1"] == "AVAILABLE"


def test_admin_bookings_are_marked_as_admin(api: TestClient, hub: dict) -> None:
    made = post(
        api,
        "bookings",
        {
            "property_id": hub["property"]["id"],
            "space_id": hub["seat2"]["id"],
            "customer_name": "Walk-in",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=1)),
        },
    )
    assert (made["channel"], made["quoted_total"], made["quote"]) == ("ADMIN", None, None)
    # Rescheduling an admin booking never invents a customer quote.
    moved = api.patch(
        f"{V1}/bookings/{made['id']}",
        json={"start_at": iso(MONDAY), "end_at": iso(MONDAY + timedelta(hours=2))},
    )
    assert moved.json()["quoted_total"] is None


def test_admin_deletes_unused_records_and_archives_those_with_history(
    api: TestClient, priya: TestClient, hub: dict, db_session: Session
) -> None:
    mine = book(priya, hub["seat1"])
    priya.post(f"{CUSTOMER}/bookings/{mine['id']}/cancel")

    # Seat 1 has a (cancelled) booking: archived, and the booking keeps its space.
    seat1 = api.delete(f"{V1}/spaces/{hub['seat1']['id']}").json()
    assert seat1 == {
        "outcome": "ARCHIVED",
        "message": "Space archived: it has history, which is kept.",
    }
    assert priya.get(f"{CUSTOMER}/bookings/{mine['id']}").json()["space_name"] == "Seat 1"
    # Seat 2 was never used: gone for good.
    assert api.delete(f"{V1}/spaces/{hub['seat2']['id']}").json()["outcome"] == "DELETED"
    assert api.get(f"{V1}/spaces/{hub['seat2']['id']}").status_code == 404
    # Customers no longer see either seat.
    states = floor_states(priya, hub, MONDAY)
    assert "Seat 1" not in states and "Seat 2" not in states

    # A tenant with nothing behind it is deleted; the customer's own record is kept.
    walk_in = post(api, "tenants", {"name": "Walk-in Co"})
    assert api.delete(f"{V1}/tenants/{walk_in['id']}").json()["outcome"] == "DELETED"
    own = db_session.scalar(select(Tenant).where(Tenant.email == "priya@example.com"))
    assert api.delete(f"{V1}/tenants/{own.id}").json()["outcome"] == "ARCHIVED"


# --- floor plans: drawn furniture, no drafts ---------------------------------------------


def test_drawn_furniture_is_validated_and_shown_to_customers(api: TestClient, hub: dict) -> None:
    layout = post(
        api,
        "layouts",
        {
            "property_id": hub["property"]["id"],
            "building_id": hub["building"]["id"],
            "floor_id": hub["floor"]["id"],
            "name": "Floor 5 plan",
            "grid_columns": 30,
            "grid_rows": 20,
        },
    )
    assert layout["status"] == "ACTIVE"
    elements = [
        {"id": "c1", "kind": "CHAIR", "x": 4, "y": 6},
        {"id": "d1", "kind": "DOOR", "x": 0, "y": 3, "width": 1, "height": 2, "rotation": 90},
        {"id": "t1", "kind": "MEETING_TABLE", "x": 10, "y": 2, "width": 4, "height": 2},
    ]
    saved = api.patch(
        f"{V1}/layouts/{layout['id']}", json={"metadata": {"elements": elements, "note": "x"}}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["metadata"]["note"] == "x"

    plan = api.get(
        f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}"
    ).json()
    drawn = plan["layouts"][0]["elements"]
    assert [(e["kind"], e["x"], e["rotation"]) for e in drawn] == [
        ("CHAIR", 4, 0),
        ("DOOR", 0, 90),
        ("MEETING_TABLE", 10, 0),
    ]

    def rejected(items: list) -> int:
        return api.patch(
            f"{V1}/layouts/{layout['id']}", json={"metadata": {"elements": items}}
        ).status_code

    assert rejected([{"id": "x", "kind": "HOT_TUB", "x": 1, "y": 1}]) == 422
    assert rejected([{"id": "x", "kind": "CHAIR", "x": -1, "y": 1}]) == 422
    assert rejected([{"id": "x", "kind": "CHAIR", "x": 1, "y": 1, "rotation": 45}]) == 422
    assert rejected([{"id": "a", "kind": "CHAIR", "x": 1, "y": 1}] * 2) == 422


def test_layouts_are_no_longer_drafted(api: TestClient, hub: dict, db_session: Session) -> None:
    body = {
        "property_id": hub["property"]["id"],
        "building_id": hub["building"]["id"],
        "floor_id": hub["floor"]["id"],
        "name": "Sketch",
    }
    assert api.post(f"{V1}/layouts", json={**body, "status": "DRAFT"}).status_code == 422
    made = post(api, "layouts", body)
    assert api.patch(f"{V1}/layouts/{made['id']}", json={"status": "DRAFT"}).status_code == 422

    # A layout saved as a draft before the change still reaches customers.
    from app.models.layout import Layout

    db_session.get(Layout, uuid.UUID(made["id"])).status = "DRAFT"
    db_session.commit()
    plan = api.get(f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}")
    assert [lay["name"] for lay in plan.json()["layouts"]] == ["Sketch"]


# --- the customer dashboard ---------------------------------------------------------------


def test_dashboard_shows_bookings_leases_and_what_the_team_did(
    api: TestClient, priya: TestClient, ravi: TestClient, hub: dict, db_session: Session
) -> None:
    waiting = book(priya, hub["seat1"], start=MONDAY + timedelta(days=1))
    confirmed = book(priya, hub["seat2"])
    api.post(f"{V1}/bookings/{confirmed['id']}/confirm")
    book(ravi, hub["seat1"])  # someone else's: never on Priya's dashboard
    tenant = db_session.scalar(select(Tenant).where(Tenant.email == "priya@example.com"))
    lease = post(
        api,
        "contracts",
        {
            "tenant_id": str(tenant.id),
            "property_id": hub["property"]["id"],
            "space_id": hub["5A"]["id"],
            "start_date": str(date.today()),
            "monthly_rent": "115500",
            "activate": True,
        },
    )

    board = priya.get(f"{CUSTOMER}/dashboard")
    assert board.status_code == 200, board.text
    data = board.json()
    assert [b["id"] for b in data["upcoming"]] == [confirmed["id"], waiting["id"]]
    assert (data["reserved"], data["booked"]) == (1, 1)
    assert [lease_due["contract"]["reference"] for lease_due in data["active_leases"]] == [
        lease["reference"]
    ]
    assert data["active_leases"][0]["next_due_date"] == str(date.today())
    assert data["monthly_rent"] == {"INR": 115500.0}
    messages = [a["message"] for a in data["activity"]]
    assert f"Booking {confirmed['reference']} was confirmed by the property team." in messages
    assert f"Booking {waiting['reference']} was made." in messages
    assert all("ravi" not in m.lower() for m in messages)

    # Each customer sees only their own; administrators use the admin portal.
    other = ravi.get(f"{CUSTOMER}/dashboard").json()
    assert (other["reserved"], other["active_leases"]) == (1, [])
    assert api.get(f"{CUSTOMER}/dashboard").status_code == 403


def test_rent_falls_due_on_the_lease_day_each_month() -> None:
    from app.services.customers import _next_monthly_date as due

    start = date(2026, 1, 31)
    assert due(start, date(2026, 4, 15), None) == date(2026, 4, 30)
    assert due(start, date(2026, 4, 30), None) == date(2026, 4, 30)
    assert due(start, date(2026, 5, 1), None) == date(2026, 5, 31)
    assert due(start, date(2026, 12, 31), None) == date(2026, 12, 31)
    assert due(date(2026, 1, 10), date(2026, 12, 11), None) == date(2027, 1, 10)
    assert due(start, date(2026, 4, 15), date(2026, 4, 20)) is None
    assert due(date(2026, 6, 15), date(2026, 6, 1), None) == date(2026, 6, 15)


def test_a_held_floor_or_portion_holds_everything_inside_it(
    api: TestClient, priya: TestClient, hub: dict
) -> None:
    def hold(space: dict, start: datetime) -> dict:
        return post(
            api,
            "bookings",
            {
                "property_id": hub["property"]["id"],
                "space_id": space["id"],
                "customer_name": "Whole-area hold",
                "start_at": iso(start),
                "end_at": iso(start + timedelta(hours=1)),
            },
        )

    # A pending hold on Portion 5A: everything in it is reserved, Portion 5B is not.
    hold(hub["5A"], MONDAY)
    states = floor_states(priya, hub, MONDAY)
    assert (states["Portion 5A"], states["Cabin 1"]) == ("RESERVED", "RESERVED")
    assert states["Seat 1"] == "AVAILABLE"

    # The whole floor made rentable and held: every space on it is reserved for that time.
    plan = priya.get(f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}")
    floor_root = next(s for s in plan.json()["spaces"] if s["space_type"] == "FLOOR")
    api.patch(f"{V1}/spaces/{floor_root['id']}", json={"is_rentable": True})
    later = MONDAY + timedelta(days=2)
    hold(floor_root, later)
    states = floor_states(priya, hub, later)
    held = {name: status for name, status in states.items() if name != "Floor 5"}
    assert held and set(held.values()) == {"RESERVED"}, held
    # Customers cannot book anything inside it for that time.
    assert book(priya, hub["seat2"], start=later, expected=409)["detail"].startswith("Reserved")


def test_free_spaces_lists_only_what_can_be_taken_by_floor(
    api: TestClient, priya: TestClient, hub: dict
) -> None:
    def free(start: datetime = MONDAY, **params: object) -> dict:
        response = priya.get(
            f"{PUBLIC}/free-spaces",
            params={"start_at": iso(start), "end_at": iso(start + timedelta(hours=1)), **params},
        )
        assert response.status_code == 200, response.text
        return response.json()

    names = lambda data: [s["name"] for f in data["floors"] for s in f["spaces"]]  # noqa: E731
    everything = free()
    assert [
        (f["property_name"], f["building_name"], f["floor_name"]) for f in everything["floors"]
    ] == [("Sigma Hub", "Tower A", "Floor 5")]
    assert set(names(everything)) == {
        "Portion 5A",
        "Portion 5B",
        "Cabin 1",
        "Open Bay",
        "Seat 1",
        "Seat 2",
    }
    seat = next(s for f in everything["floors"] for s in f["spaces"] if s["name"] == "Seat 1")
    assert seat["inside"] == "Open Bay" and seat["bookable"] is True
    assert seat["from_price"] == {"billing_period": "HOURLY", "amount": 100.0, "currency": "INR"}

    # Seat 1 booked (pending) and Portion 5A held: they, what they hold, and what holds
    # them, drop out.
    book(priya, hub["seat1"])
    post(
        api,
        "bookings",
        {
            "property_id": hub["property"]["id"],
            "space_id": hub["5A"]["id"],
            "customer_name": "Hold",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=1)),
        },
    )
    now_free = free()
    # Portion 5B holds the booked seat, so it cannot be taken whole either; Seat 2 still can.
    assert names(now_free) == ["Seat 2"]
    assert now_free["total"] == 1
    # Another time is unaffected.
    assert "Seat 1" in names(free(MONDAY + timedelta(days=1)))
    # Filters.
    assert names(free(space_type="SEAT")) == ["Seat 2"]
    assert free(city="Nowhere")["floors"] == []
    # An unlisted property shows nothing.
    api.patch(f"{V1}/properties/{hub['property']['id']}", json={"is_published": False})
    assert free()["floors"] == []


# --- reservation -> contract request -> approved or rejected --------------------------------


def test_a_reservation_becomes_a_contract_request_that_holds_the_space(
    api: TestClient, priya: TestClient, ravi: TestClient, hub: dict
) -> None:
    mine = book(priya, hub["seat1"])
    start = (MONDAY + timedelta(days=3)).date()
    request = priya.post(
        f"{CUSTOMER}/bookings/{mine['id']}/contract",
        json={"start_date": str(start), "months": 3, "notes": "Team of one"},
    )
    assert request.status_code == 201, request.text
    contract = request.json()
    assert contract["status"] == "PENDING" and contract["space_name"] == "Seat 1"
    assert (contract["start_date"], contract["end_date"]) == (
        str(start),
        str(start.replace(month=start.month + 3) - timedelta(days=1))
        if start.month <= 9
        else contract["end_date"],
    )
    # The reservation was replaced by the request.
    assert priya.get(f"{CUSTOMER}/bookings/{mine['id']}").json()["status"] == "CANCELLED"
    assert priya.get(f"{CUSTOMER}/dashboard").json()["contract_requests"] == 1

    # While it waits, no one else sees or takes Seat 1 for those dates.
    during = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(days=10, hours=5)
    assert floor_states(ravi, hub, during)["Seat 1"] == "RESERVED"
    assert book(ravi, hub["seat1"], start=during, expected=409)["detail"].startswith("Reserved")
    free = ravi.get(
        f"{PUBLIC}/free-spaces",
        params={"start_at": iso(during), "end_at": iso(during + timedelta(hours=1))},
    ).json()
    assert "Seat 1" not in [s["name"] for f in free["floors"] for s in f["spaces"]]

    # The property team sees it as a pending contract and rejects it: the seat is free again.
    listed = api.get(f"{V1}/contracts", params={"status": "PENDING"}).json()["items"]
    assert [c["reference"] for c in listed] == [contract["reference"]]
    assert listed[0]["holds_inventory"] is True
    rejected = api.post(f"{V1}/contracts/{contract['id']}/cancel", json={"reason": "Not now"})
    assert rejected.status_code == 200, rejected.text
    assert floor_states(ravi, hub, during)["Seat 1"] == "AVAILABLE"
    mine_now = priya.get(f"{CUSTOMER}/contracts").json()[0]
    assert mine_now["status"] == "CANCELLED"
    messages = [a["message"] for a in priya.get(f"{CUSTOMER}/dashboard").json()["activity"]]
    assert f"Lease {contract['reference']} was cancelled by the property team." in messages


def test_a_contract_request_can_be_approved_or_withdrawn(
    api: TestClient, priya: TestClient, ravi: TestClient, hub: dict
) -> None:
    start = (MONDAY + timedelta(days=3)).date()
    first = book(priya, hub["seat1"])
    approved = priya.post(
        f"{CUSTOMER}/bookings/{first['id']}/contract", json={"start_date": str(start), "months": 1}
    ).json()
    assert api.post(f"{V1}/contracts/{approved['id']}/activate").json()["status"] == "ACTIVE"
    during = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(days=5, hours=5)
    assert floor_states(ravi, hub, during)["Seat 1"] == "OCCUPIED"
    # An approved contract can no longer be withdrawn by the customer.
    assert priya.post(f"{CUSTOMER}/contracts/{approved['id']}/withdraw").status_code == 409

    second = book(priya, hub["seat2"])
    waiting = priya.post(
        f"{CUSTOMER}/bookings/{second['id']}/contract", json={"start_date": str(start), "months": 1}
    ).json()
    assert ravi.post(f"{CUSTOMER}/contracts/{waiting['id']}/withdraw").status_code == 404
    withdrawn = priya.post(f"{CUSTOMER}/contracts/{waiting['id']}/withdraw")
    assert withdrawn.json()["status"] == "CANCELLED"
    assert floor_states(ravi, hub, during)["Seat 2"] == "AVAILABLE"

    # A booking that is already cancelled cannot be turned into a contract.
    assert (
        priya.post(
            f"{CUSTOMER}/bookings/{second['id']}/contract",
            json={"start_date": str(start), "months": 1},
        ).status_code
        == 409
    )


def test_contract_terms_end_the_day_before_the_same_date() -> None:
    from app.services.customers import _add_months

    assert _add_months(date(2026, 1, 1), 12) == date(2026, 12, 31)
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 27)
    assert _add_months(date(2026, 11, 15), 3) == date(2027, 2, 14)


# --- email code sign-in --------------------------------------------------------------------


class OtpGateway:
    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        self.key, self.sent, self.users = key, [], {}

    def send_otp(self, email: str) -> None:
        self.sent.append(email)
        self.users.setdefault(email, uuid.uuid4())

    def verify_otp(self, email: str, code: str) -> SupabaseSession:
        from app.core.errors import UnauthorizedError

        if code != "123456" or email not in self.users:
            raise UnauthorizedError("That code is wrong or has expired. Ask for a new one.")
        token = make_token(self.key, self.users[email], email=email)
        return SupabaseSession(token, "refresh", 3600)


def test_customers_sign_in_with_an_emailed_code(
    verifier: None, db_session: Session, signing_key
) -> None:
    gateway = OtpGateway(signing_key)
    app.dependency_overrides[get_auth_gateway] = lambda: gateway
    try:
        client = TestClient(app)
        assert (
            client.post(f"{CUSTOMER}/otp/request", json={"email": "New@Example.com"}).status_code
            == 202
        )
        assert gateway.sent == ["new@example.com"]
        wrong = client.post(
            f"{CUSTOMER}/otp/verify", json={"email": "new@example.com", "code": "000000"}
        )
        assert wrong.status_code == 401
        first = client.post(
            f"{CUSTOMER}/otp/verify",
            json={"email": "new@example.com", "code": "123456", "full_name": "Meera"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["user"]["role"] == "CUSTOMER"
        tenant = db_session.scalar(select(Tenant).where(Tenant.email == "new@example.com"))
        assert tenant is not None and tenant.name == "Meera"
        # Signing in again uses the same account and tenant.
        again = client.post(
            f"{CUSTOMER}/otp/verify", json={"email": "new@example.com", "code": "123456"}
        )
        assert again.json()["user"]["id"] == first.json()["user"]["id"]
        assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1
        # An administrator cannot sign in to the customer app with a code.
        admin_id = uuid.uuid4()
        db_session.add(AppUser(id=admin_id, email="boss@example.com", role=UserRole.ADMIN))
        db_session.commit()
        gateway.users["boss@example.com"] = admin_id
        refused = client.post(
            f"{CUSTOMER}/otp/verify", json={"email": "boss@example.com", "code": "123456"}
        )
        assert refused.status_code == 403
        assert (
            client.post(
                f"{CUSTOMER}/otp/verify", json={"email": "x@example.com", "code": "12ab56"}
            ).status_code
            == 422
        )
    finally:
        app.dependency_overrides.pop(get_auth_gateway, None)
