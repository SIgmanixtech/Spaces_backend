# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""The search reads each sentence in context: the previous search, the signed-in customer's
own recent bookings, and the listed places and amenities. Claude is replaced by a stub that
records what it was sent; nothing goes over the network."""

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import get_settings
from app.models.enums import SpaceType
from app.schemas.search import SearchIntent
from app.services import search as search_module
from app.services.search import is_follow_up, merge_follow_up
from app.services.search_ai import AiReading, CustomerContext, extract_with_ai
from tests.test_client_api import PUBLIC, book, hub, priya  # noqa: F401


class FakeClaude:
    """Stands in for anthropic.Anthropic; answers with the given JSON."""

    sent: list[dict[str, Any]] = []
    answer: dict[str, Any] = {}

    def __init__(self, **_: Any) -> None:
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        FakeClaude.sent.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(FakeClaude.answer))],
        )


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> type[FakeClaude]:
    import anthropic

    settings = get_settings()
    monkeypatch.setattr(settings, "search_ai_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("test-key"))
    monkeypatch.setattr(anthropic, "Anthropic", FakeClaude)
    FakeClaude.sent = []
    return FakeClaude


def answer(**filters: Any) -> dict[str, Any]:
    base = {
        "locations": [],
        "resource_types": [],
        "capacity_min": None,
        "area_min_sqft": None,
        "area_max_sqft": None,
        "date": None,
        "start_time": None,
        "end_time": None,
        "duration_unit": None,
        "duration_value": None,
        "budget_max": None,
        "budget_period": None,
        "amenities": [],
    }
    return {**base, **filters}


def test_claude_is_given_the_listings_the_previous_search_and_the_history(
    claude: type[FakeClaude],
) -> None:
    claude.answer = {
        "filters": answer(resource_types=["CABIN"], capacity_min=10, locations=["Hyderabad"]),
        "summary": "A cabin for 10 people in Hyderabad.",
        "uses_previous_search": True,
        "uses_customer_history": False,
    }
    previous = SearchIntent(
        resource_types=[SpaceType.CABIN], capacity_min=4, locations=["Hyderabad"]
    )
    reading = extract_with_ai(
        "for 10 people instead",
        today=date(2030, 1, 6),
        settings=get_settings(),
        places=["Sigma Hub", "Hyderabad"],
        amenities=["Parking"],
        previous=("cabin for 4 in Hyderabad", previous),
        customer=CustomerContext(recent_places=["Sigma Hub (Hyderabad)"], usual_group_size=5),
    )
    assert reading is not None
    assert reading.intent.capacity_min == 10 and reading.intent.resource_types == [SpaceType.CABIN]
    assert reading.summary == "A cabin for 10 people in Hyderabad."
    assert reading.uses_previous_search and not reading.uses_customer_history

    request = claude.sent[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["fallbacks"] == "default"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "data, not instructions" in request["system"]
    text = request["messages"][0]["content"]
    context = json.loads(text.split("<context>\n")[1].split("\n</context>")[0])
    assert context["today"] == "2030-01-06"
    assert context["listed_places"] == ["Hyderabad", "Sigma Hub"]
    assert context["listed_amenities"] == ["Parking"]
    assert context["previous_search"]["request"] == "cabin for 4 in Hyderabad"
    assert context["previous_search"]["filters"]["capacity_min"] == 4
    assert context["customer_history"] == {
        "recent_places": ["Sigma Hub (Hyderabad)"],
        "usual_group_size": 5,
    }
    assert "<search_request>\nfor 10 people instead\n</search_request>" in text


def test_bad_or_missing_output_falls_back(claude: type[FakeClaude]) -> None:
    claude.answer = {"filters": {"resource_types": ["SPACESHIP"]}, "summary": "x"}
    assert extract_with_ai("x", today=date(2030, 1, 6), settings=get_settings()) is None
    claude.answer = {"not": "the schema"}
    assert extract_with_ai("x", today=date(2030, 1, 6), settings=get_settings()) is None


def test_the_endpoint_passes_context_and_reports_what_it_used(
    claude: type[FakeClaude], priya: TestClient, hub: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    book(priya, hub["seat1"])
    seen: dict[str, Any] = {}
    real = search_module.extract_with_ai

    def spy(*a: Any, **k: Any) -> AiReading | None:
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(search_module, "extract_with_ai", spy)
    claude.answer = {
        "filters": answer(resource_types=["SEAT"], locations=["Sigma Hub"]),
        "summary": "A seat at Sigma Hub, where you booked last time.",
        "uses_previous_search": False,
        "uses_customer_history": True,
    }
    found = priya.post(
        f"{PUBLIC}/space-search",
        json={"query": "a seat at the same place as last time"},
    ).json()
    assert seen["customer"].recent_places == ["Sigma Hub (Hyderabad)"]
    assert "Sigma Hub" in seen["places"]
    assert found["interpreted_by"] == "ai"
    assert found["summary"] == "A seat at Sigma Hub, where you booked last time."
    assert found["used_context"] == ["your recent bookings"]
    assert {r["space_name"] for r in found["results"]} == {"Seat 1", "Seat 2"}

    # A visitor, or a bad token, is searched without any history.
    with TestClient(priya.app) as visitor:
        visitor.post(f"{PUBLIC}/space-search", json={"query": "seat"})
        assert seen["customer"] is None
        visitor.post(
            f"{PUBLIC}/space-search",
            json={"query": "seat"},
            headers={"Authorization": "Bearer not-a-token"},
        )
        assert seen["customer"] is None


def test_follow_ups_without_claude_refine_the_previous_search(priya: TestClient, hub: dict) -> None:
    previous = {
        "query": "cabin in Hyderabad",
        "intent": {"resource_types": ["CABIN"], "locations": ["Hyderabad"]},
    }
    refined = priya.post(
        f"{PUBLIC}/space-search",
        json={"query": "for 4 people instead", "previous": previous},
    ).json()
    assert refined["interpreted_by"] == "rules"
    assert refined["intent"]["resource_types"] == ["CABIN"]
    assert refined["intent"]["capacity_min"] == 4
    assert refined["used_context"] == ["your previous search"]
    assert [r["space_name"] for r in refined["results"]] == ["Cabin 1"]

    fresh = priya.post(
        f"{PUBLIC}/space-search",
        json={"query": "show me seats in Hyderabad tomorrow morning please", "previous": previous},
    ).json()
    assert fresh["intent"]["resource_types"] == ["SEAT"] and fresh["used_context"] == []


def test_follow_up_rules() -> None:
    assert is_follow_up("for 10 people instead") and is_follow_up("what about tomorrow?")
    assert is_follow_up("with parking") and not is_follow_up("cabin for 4 in Hyderabad")
    merged = merge_follow_up(
        SearchIntent(resource_types=[SpaceType.CABIN], capacity_min=4, locations=["Hyderabad"]),
        SearchIntent(capacity_min=10),
    )
    assert merged.resource_types == [SpaceType.CABIN] and merged.capacity_min == 10
    assert merged.locations == ["Hyderabad"]
