from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from tests.test_properties import PROPERTIES, create_property
from tests.test_resources import V1, post

DASHBOARD = f"{V1}/property/dashboard"


def space(api: TestClient, prop: dict, name: str) -> dict:
    return post(api, "spaces", {"property_id": prop["id"], "name": name, "space_type": "DESK"})


def test_empty_database_reports_real_zeros(api: TestClient) -> None:
    summary = api.get(f"{DASHBOARD}/summary")

    assert summary.status_code == 200
    assert summary.json() == {
        "total": 0,
        "vacant": 0,
        "reserved": 0,
        "occupied": 0,
        "unavailable": 0,
        "spaces": {
            "total": 0,
            "available": 0,
            "booked": 0,
            "reserved": 0,
            "occupied": 0,
            "unavailable": 0,
        },
        "occupancy_rate": None,
        "occupancy_trend": [],
    }
    assert api.get(f"{DASHBOARD}/activity").json() == []
    assert api.get(f"{DASHBOARD}/availability").json() == []


def test_summary_counts_each_property_once_by_derived_status(api: TestClient) -> None:
    vacant = create_property(api, name="Vacant House")
    reserved = create_property(api, name="Reserved House")
    occupied = create_property(api, name="Occupied House")
    create_property(api, name="Closed House", status="UNDER_MAINTENANCE")
    archived = create_property(api, name="Gone House")
    api.delete(f"{PROPERTIES}/{archived['id']}")

    space(api, vacant, "V1")
    reserved_desk = space(api, reserved, "R1")
    occupied_desk = space(api, occupied, "O1")
    space(api, occupied, "O2")

    start = datetime.now(UTC) + timedelta(days=3)
    post(
        api,
        "bookings",
        {
            "property_id": reserved["id"],
            "space_id": reserved_desk["id"],
            "customer_name": "Priya Nair",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=2)).isoformat(),
        },
    )
    tenant = post(api, "tenants", {"name": "Acme Ltd"})
    post(
        api,
        f"tenants/{tenant['id']}/occupancies",
        {
            "property_id": occupied["id"],
            "space_id": occupied_desk["id"],
            "start_date": date.today().isoformat(),
        },
    )

    summary = api.get(f"{DASHBOARD}/summary").json()

    assert summary["total"] == 4
    assert (
        summary["vacant"],
        summary["reserved"],
        summary["occupied"],
        summary["unavailable"],
    ) == (
        1,
        1,
        1,
        1,
    )
    assert summary["spaces"] == {
        "total": 4,
        "available": 2,
        "booked": 0,
        "reserved": 1,
        "occupied": 1,
        "unavailable": 0,
    }
    assert summary["occupancy_rate"] == 25.0
    assert summary["occupancy_trend"][-1]["occupancy_rate"] == 25.0
    assert summary["occupancy_trend"][-1]["period"] == date.today().strftime("%b %Y")

    # The list filters agree with the dashboard counts.
    for status in ("VACANT", "RESERVED", "OCCUPIED", "UNAVAILABLE"):
        listed = api.get(PROPERTIES, params={"occupancy_status": status}).json()
        assert listed["total"] == 1, status
    by_tenant = api.get(PROPERTIES, params={"tenant": "acme"}).json()
    assert [item["name"] for item in by_tenant["items"]] == ["Occupied House"]
    assert api.get(PROPERTIES, params={"city": "hyder", "state": "telan"}).json()["total"] == 4

    at_property = api.get(f"{V1}/tenants", params={"property_id": occupied["id"]}).json()
    assert [item["name"] for item in at_property["items"]] == ["Acme Ltd"]
    assert api.get(f"{V1}/tenants", params={"property_id": vacant["id"]}).json()["total"] == 0
    assert api.get(f"{V1}/tenants", params={"occupancy_status": "OCCUPIED"}).json()["total"] == 1
    assert api.get(f"{V1}/tenants", params={"occupancy_status": "VACANT"}).json()["total"] == 0


def test_availability_lists_free_spaces_per_open_property(api: TestClient) -> None:
    alpha = create_property(api, name="Alpha")
    create_property(api, name="No Spaces")
    closed = create_property(api, name="Closed", status="INACTIVE")
    space(api, alpha, "A1")
    broken = space(api, alpha, "A2")
    space(api, closed, "C1")
    api.patch(f"{V1}/spaces/{broken['id']}", json={"operational_status": "UNDER_MAINTENANCE"})

    availability = api.get(f"{DASHBOARD}/availability").json()

    assert availability == [
        {
            "property_id": alpha["id"],
            "property_name": "Alpha",
            "available_spaces": 1,
            "total_spaces": 2,
        }
    ]


def test_activity_is_newest_first_and_limited(api: TestClient) -> None:
    prop = create_property(api, name="Alpha")
    api.patch(f"{PROPERTIES}/{prop['id']}", json={"city": "Pune"})
    space(api, prop, "A1")

    activity = api.get(f"{DASHBOARD}/activity").json()

    assert [entry["action"] for entry in activity] == ["created", "updated", "created"]
    assert activity[0]["message"] == 'Space "A1" was created.'
    assert activity[0]["property_name"] == "Alpha"
    assert len(api.get(f"{DASHBOARD}/activity", params={"limit": 2}).json()) == 2
    assert api.get(f"{DASHBOARD}/activity", params={"limit": 500}).status_code == 422


def test_dashboard_requires_authentication(client: TestClient) -> None:
    for path in ("summary", "activity", "availability"):
        assert client.get(f"{DASHBOARD}/{path}").status_code == 401
