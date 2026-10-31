# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Business rule D20: a space's old list price (price_amount / price_unit) is reference only.
Customers never receive it from any public endpoint; admins still see it, unchanged."""

from fastapi.testclient import TestClient

from app.main import app
from tests.test_client_api import MONDAY, PUBLIC, hub, iso  # noqa: F401
from tests.test_resources import V1


def test_customers_never_receive_the_old_list_price(api: TestClient, hub: dict) -> None:
    cabin = hub["cabin"]
    saved = api.patch(
        f"{V1}/spaces/{cabin['id']}", json={"price_amount": 4321, "price_unit": "DAY"}
    )
    assert saved.status_code == 200, saved.text
    prop, floor = hub["property"]["id"], hub["floor"]["id"]
    visitor = TestClient(app)
    window = {"start_at": iso(MONDAY), "end_at": iso(MONDAY.replace(hour=MONDAY.hour + 1))}
    answers = [
        visitor.get(f"{PUBLIC}/properties"),
        visitor.get(f"{PUBLIC}/properties/{prop}"),
        visitor.get(f"{PUBLIC}/properties/{prop}/floors/{floor}"),
        visitor.get(f"{PUBLIC}/properties/{prop}/floors/{floor}/availability", params=window),
        visitor.get(f"{PUBLIC}/free-spaces", params=window),
        visitor.post(f"{PUBLIC}/space-search", json={"query": "cabin"}),
    ]
    for answer in answers:
        assert answer.status_code == 200, answer.text
        assert "price_amount" not in answer.text and "price_unit" not in answer.text
        assert "4321" not in answer.text
    # The admin record keeps it as it was (nothing deleted or migrated).
    admin_view = api.get(f"{V1}/spaces/{cabin['id']}").json()
    assert (admin_view["price_amount"], admin_view["price_unit"]) == (4321.0, "DAY")
    pricing = api.get(f"{V1}/properties/{prop}/pricing").json()
    legacy = [s["legacy_price"] for s in pricing["spaces"] if s["space_id"] == cabin["id"]]
    assert legacy == [{"amount": 4321.0, "unit": "DAY", "currency": "INR"}]
