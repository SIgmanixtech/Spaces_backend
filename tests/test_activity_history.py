# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""The admin activity history (Phase 2e, BR-033): read-only over the stored activity log.

Sample property "Sigma Hub" (from test_client_api): Tower A > Floor 5 > Portion 5B >
Open Bay > Seat 1."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import ActivityLog
from app.models.enums import UserRole
from app.models.user import AppUser
from app.services.activity import recorded_changes
from tests.test_client_api import book, hub, priya  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1

ACT = f"{V1}/admin/activity"
ADMIN = f"{V1}/admin"


def entries(api: TestClient, **params: object) -> dict:
    response = api.get(ACT, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def log(session: Session, **values: object) -> ActivityLog:
    entry = ActivityLog(
        action=values.pop("action", "created"),
        entity_type=values.pop("entity_type", "spaces"),
        message=values.pop("message", "Something happened."),
        **values,
    )
    session.add(entry)
    session.commit()
    return entry


def test_only_admins_read_the_history(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        assert who.get(ACT).status_code == status
        assert who.get(f"{ACT}/options").status_code == status


def test_entries_keep_their_stored_message_and_gain_readable_labels(
    api: TestClient, hub: dict, admin_user: AppUser, db_session: Session
) -> None:
    newest = entries(api, page_size=5)["items"][0]
    stored = db_session.get(ActivityLog, uuid.UUID(newest["id"]))
    assert newest["message"] == stored.message  # exactly as recorded
    assert newest["action"] == stored.action and newest["entity_type"] == stored.entity_type
    assert newest["created_at"].startswith(
        stored.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")
    )
    seat = entries(api, entity_id=hub["seat1"]["id"])["items"]
    assert [(i["summary"], i["message"]) for i in seat] == [
        ("Space: created", 'Space "Seat 1" was created.')
    ]
    item = seat[0]
    assert item["actor"] == {
        "id": str(admin_user.id),
        "name": admin_user.full_name,
        "email": admin_user.email,
        "role": "ADMIN",
    }
    assert (item["property_id"], item["property_name"]) == (hub["property"]["id"], "Sigma Hub")
    assert item["record"] == {"state": "EXISTS", "name": "Seat 1"}
    assert item["changes"] == []


def test_current_context_is_resolved_from_records_as_they_are_now(
    api: TestClient, hub: dict, priya: TestClient
) -> None:
    def context(entity_id: str) -> dict:
        return entries(api, entity_id=entity_id)["items"][0]["current_context"]

    assert context(hub["seat1"]["id"]) == {
        "building_id": hub["building"]["id"],
        "building_name": "Tower A",
        "floor_id": hub["floor"]["id"],
        "floor_name": "Floor 5",
        "space_id": hub["seat1"]["id"],
        "space_name": "Seat 1",
    }
    floor = context(hub["floor"]["id"])
    assert (floor["building_name"], floor["floor_name"], floor["space_id"]) == (
        "Tower A",
        "Floor 5",
        None,
    )
    assert context(hub["building"]["id"])["building_name"] == "Tower A"
    assert entries(api, entity_id=hub["property"]["id"])["items"][0]["current_context"] is None
    # A booking names only its space; the floor and building come from that space.
    booked = book(priya, hub["seat1"])
    found = context(booked["id"])
    assert (found["building_name"], found["floor_name"], found["space_name"]) == (
        "Tower A",
        "Floor 5",
        "Seat 1",
    )
    # Renaming the space later changes its current context, never the stored message.
    api.patch(f"{ADMIN}/spaces/{hub['seat1']['id']}", json={"name": "Seat 1 (window)"})
    created = [
        i for i in entries(api, entity_id=hub["seat1"]["id"])["items"] if i["action"] == "created"
    ][0]
    assert created["message"] == 'Space "Seat 1" was created.'
    assert created["current_context"]["space_name"] == "Seat 1 (window)"


def test_deleted_and_unknown_records_actors_and_properties_do_not_break_the_view(
    api: TestClient, admin_user: AppUser, db_session: Session
) -> None:
    prop = create_property(api, name="Short Lived")
    building = api.post(f"{ADMIN}/buildings", json={"property_id": prop["id"], "name": "B"}).json()
    floor = api.post(
        f"{ADMIN}/floors", json={"building_id": building["id"], "name": "F1", "level_number": 1}
    ).json()
    assert api.delete(f"{ADMIN}/floors/{floor['id']}").status_code == 204
    gone = entries(api, entity_id=floor["id"])["items"]
    assert {i["action"] for i in gone} == {"created", "deleted"}
    assert all(i["record"] == {"state": "DELETED", "name": None} for i in gone)
    assert all(i["current_context"] is None for i in gone)
    # A property with spaces is archived, not deleted.
    api.post(f"{ADMIN}/spaces", json={"property_id": prop["id"], "name": "D", "space_type": "DESK"})
    api.delete(f"{ADMIN}/properties/{prop['id']}")
    archived = entries(api, entity_id=prop["id"], action="created")["items"][0]
    assert archived["record"] == {"state": "ARCHIVED", "name": "Short Lived"}

    # Unknown kinds and actions, no record, no property, no actor.
    log(db_session, entity_type="legacy_thing", action="frobnicated", message="Old entry.")
    odd = entries(api, entity_type="legacy_thing")["items"][0]
    assert (odd["entity_label"], odd["action_label"], odd["summary"]) == (
        "Legacy thing",
        "Frobnicated",
        "Legacy thing: frobnicated",
    )
    assert odd["record"]["state"] == "UNKNOWN" and odd["actor"] is None
    assert (odd["property_id"], odd["property_name"], odd["message"]) == (None, None, "Old entry.")
    # An entry whose user was removed keeps its message; the actor becomes unknown.
    user = AppUser(id=uuid.uuid4(), email="gone@example.com", role=UserRole.ADMIN)
    db_session.add(user)
    db_session.commit()
    mine = log(db_session, actor_id=user.id, message="By a user who later left.")
    db_session.delete(user)
    db_session.commit()
    left = entries(api, search="who later left")["items"][0]
    assert (left["id"], left["actor"], left["message"]) == (
        str(mine.id),
        None,
        "By a user who later left.",
    )


def test_newest_first_with_a_stable_tie_breaker_and_exact_pages(
    api: TestClient, db_session: Session
) -> None:
    at = datetime(2030, 1, 5, 6, 0, tzinfo=UTC)
    made = [
        log(db_session, entity_type="tie_case", message=f"Tie {n}", created_at=at) for n in range(5)
    ]
    expected = sorted((str(e.id) for e in made), reverse=True)
    pages = [entries(api, entity_type="tie_case", page=p, page_size=2) for p in (1, 2, 3)]
    assert [p["total"] for p in pages] == [5, 5, 5]
    assert [i["id"] for p in pages for i in p["items"]] == expected  # no overlap, no gap
    assert entries(api, entity_type="tie_case", page=4, page_size=2)["items"] == []
    whole = entries(api, page_size=100)["items"]
    times = [i["created_at"] for i in whole]
    assert times == sorted(times, reverse=True)


def test_every_filter_narrows_the_list(
    api: TestClient, hub: dict, admin_user: AppUser, db_session: Session
) -> None:
    other = create_property(api, name="Elsewhere")
    pid = hub["property"]["id"]
    assert all(
        i["property_id"] == pid for i in entries(api, property_id=pid, page_size=100)["items"]
    )
    assert entries(api, property_id=other["id"])["total"] == 1
    floors = entries(api, entity_type="floors", page_size=100)["items"]
    assert floors and all(i["entity_type"] == "floors" for i in floors)
    assert all(
        i["action"] == "created" for i in entries(api, action="created", page_size=100)["items"]
    )
    mine = entries(api, actor_id=str(admin_user.id), page_size=100)
    assert mine["total"] >= 10 and all(
        i["actor"]["id"] == str(admin_user.id) for i in mine["items"]
    )
    assert entries(api, actor_id=str(uuid.uuid4()))["total"] == 0
    assert (
        entries(api, entity_id=hub["bay"]["id"])["items"][0]["message"]
        == 'Space "Open Bay" was created.'
    )
    found = entries(api, search="open bay")
    assert found["total"] == 1 and found["items"][0]["entity_id"] == hub["bay"]["id"]

    # Dates are calendar days in the business time zone (India, UTC+5:30), both ends included.
    late = log(
        db_session,
        entity_type="dated",
        message="Late on 9 Jan IST",
        created_at=datetime(2030, 1, 9, 18, 0, tzinfo=UTC),
    )  # 23:30 IST, 9 Jan
    early = log(
        db_session,
        entity_type="dated",
        message="Early on 10 Jan IST",
        created_at=datetime(2030, 1, 9, 19, 0, tzinfo=UTC),
    )  # 00:30 IST, 10 Jan
    on = lambda **d: [i["id"] for i in entries(api, entity_type="dated", **d)["items"]]  # noqa: E731
    assert on(date_from="2030-01-10", date_to="2030-01-10") == [str(early.id)]
    assert on(date_from="2030-01-09", date_to="2030-01-09") == [str(late.id)]
    assert on(date_from="2030-01-09") == [str(early.id), str(late.id)]
    assert on(date_to="2030-01-08") == []
    assert (
        api.get(ACT, params={"date_from": "2030-01-10", "date_to": "2030-01-09"}).status_code == 400
    )


def test_options_come_from_stored_entries_and_existing_records(
    api: TestClient, hub: dict, admin_user: AppUser, db_session: Session
) -> None:
    log(db_session, entity_type="legacy_thing", action="frobnicated")
    options = api.get(f"{ACT}/options").json()
    types = {o["value"]: o["label"] for o in options["entity_types"]}
    assert types["spaces"] == "Space" and types["legacy_thing"] == "Legacy thing"
    assert "contracts" not in types  # nothing recorded for contracts here
    assert {"value": "created", "label": "Created"} in options["actions"]
    assert [o["value"] for o in options["actors"]] == [str(admin_user.id)]
    assert {"value": hub["property"]["id"], "label": "Sigma Hub"} in options["properties"]


def test_reimbursement_changes_are_read_only_from_the_exact_recorded_formats(
    api: TestClient, db_session: Session
) -> None:
    made = api.post(f"{ADMIN}/reimbursements/expense-types", json={"name": "Pest control"}).json()
    api.patch(f"{ADMIN}/reimbursements/expense-types/{made['id']}", json={"name": "Pest service"})
    renamed = entries(api, entity_type="reimbursement_expense_types", action="updated")["items"][0]
    assert (
        renamed["message"]
        == 'Expense type "Pest control" updated: name Pest control -> Pest service.'
    )
    assert renamed["changes"] == [
        {"field": "name", "before": "Pest control", "after": "Pest service"}
    ]

    correction = (
        "Expense corrected before review: amount 2000.00 -> 2200; description Water -> Water bill."
    )
    assert [
        c.model_dump() for c in recorded_changes("reimbursement_expenses", "updated", correction)
    ] == [
        {"field": "amount", "before": "2000.00", "after": "2200"},
        {"field": "description", "before": "Water", "after": "Water bill"},
    ]
    not_read = [
        (
            "reimbursement_expenses",
            "updated",
            "Expense corrected before review: colour red -> blue.",
        ),
        (
            "reimbursement_expenses",
            "updated",
            "Expense corrected before review: description a -> b -> c.",
        ),
        (
            "reimbursement_expenses",
            "updated",
            "Expense corrected before review: amount 1 -> 2",
        ),  # no stop
        ("reimbursement_expenses", "reviewed", "Expense corrected before review: amount 1 -> 2."),
        ("spaces", "updated", "Expense corrected before review: amount 1 -> 2."),
        ("reimbursement_terms", "updated", "Reimbursement term updated."),
        ("spaces", "updated", 'Space "5A" was updated.'),
        (
            "reimbursement_expenses",
            "updated",
            "Expense corrected before review: amount 1 -> 2." + "x" * 450,
        ),
    ]
    for entity_type, action, message in not_read:
        assert recorded_changes(entity_type, action, message) == [], message
    # A message cut at 500 characters is not read, and is shown as stored.
    cut = ("Expense corrected before review: description " + "y" * 200 + " -> " + "z" * 300)[:500]
    entry = log(db_session, entity_type="reimbursement_expenses", action="updated", message=cut)
    shown = entries(api, search="y" * 50)["items"][0]
    assert (shown["id"], shown["changes"], shown["message"]) == (str(entry.id), [], cut)


@pytest.mark.parametrize("size", [5, 40])
def test_a_page_takes_a_fixed_number_of_queries(
    api: TestClient, hub: dict, db_session: Session, size: int
) -> None:
    from app.db.session import get_engine

    statements: list[str] = []

    def count(*args: object) -> None:
        statements.append(str(args[2]))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", count)
    try:
        page = entries(api, page_size=size)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(page["items"]) == min(size, page["total"])
    kinds = {i["entity_type"] for i in page["items"]}
    # Auth and user lookup, page + count, actors, properties, one per kind, three names.
    assert len([s for s in statements if "activity_log" in s]) == 2
    assert len(statements) <= 6 + len(kinds) + 4
    assert db_session.scalar(select(ActivityLog.id).limit(1)) is not None
