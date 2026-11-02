# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""CRM core API (Phase 3A). Needs the migrated test database (like every API test here).

Sample property "Sigma Hub" (from test_client_api): Tower A > Floor 5 > Portion 5A (Cabin 1),
Portion 5B (Open Bay > Seat 1, Seat 2)."""

import uuid
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ActivityLog, Lead, Tenant
from app.models.enums import UserRole
from app.models.user import AppUser
from app.services.occupancy import business_today
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1

CRM = f"{V1}/admin/crm"


def lead(api: TestClient, expected: int = 201, **body: object) -> dict:
    response = api.post(f"{CRM}/leads", json={"company_name": "Acme Ltd", **body})
    assert response.status_code == expected, response.text
    return response.json()


def test_only_admins_reach_crm(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        assert who.get(f"{CRM}/leads").status_code == status
        assert who.post(f"{CRM}/leads", json={"company_name": "X"}).status_code == status
        assert who.get(f"{CRM}/tasks").status_code == status
        assert who.post(f"{CRM}/leads/check-duplicates", json={}).status_code == status
    # Nothing CRM-related is reachable through the client prefix.
    assert priya.get(f"{V1}/client/crm/leads").status_code == 404


def test_a_lead_is_created_with_a_reference_source_and_first_stage(
    api: TestClient, hub: dict, admin_user: AppUser
) -> None:
    made = lead(
        api,
        contact_name="Priya Nair",
        contact_email="Priya@Example.com",
        contact_phone="98765 43210",
        source="META_ADS",
        property_id=hub["property"]["id"],
        space_type="CABIN",
        capacity=4,
        area_sqft="200",
        budget_amount="50000",
        budget_period="MONTHLY",
        desired_start_date=(business_today() + timedelta(days=30)).isoformat(),
        owner_id=str(admin_user.id),
    )
    assert made["reference"].startswith(f"LD-{business_today().year}-")
    assert (made["source"], made["stage"], made["next_stage"]) == (
        "META_ADS",
        "LEAD_CREATED",
        "LEAD_REVIEWED",
    )
    assert (made["property_name"], made["owner_email"]) == ("Sigma Hub", admin_user.email)
    second = lead(api, company_name="Beta LLP")
    assert second["reference"] > made["reference"]
    # The source can be corrected (approved 2 Nov 2026), recorded old -> new; a lead always
    # has one. The stage has its own endpoint.
    corrected = api.patch(f"{CRM}/leads/{made['id']}", json={"source": "EXCEL"})
    assert corrected.status_code == 200 and corrected.json()["source"] == "EXCEL"
    assert api.patch(f"{CRM}/leads/{made['id']}", json={"source": None}).status_code == 422
    history = api.get(
        f"{V1}/admin/activity", params={"entity_type": "leads", "entity_id": made["id"]}
    ).json()["items"]
    assert any(e["message"].endswith("source changed from Meta Ads to Excel.") for e in history)
    assert (
        api.patch(f"{CRM}/leads/{made['id']}", json={"stage": "SPACE_SELECTED"}).status_code == 422
    )


def test_lead_validation(api: TestClient, hub: dict, db_session: Session) -> None:
    lead(api, 422, company_name=None, contact_name=None, contact_email="a@example.com")
    lead(api, 422, budget_amount="1000")  # amount without its period
    lead(api, 422, contact_email="not-an-email")
    customer = AppUser(id=uuid.uuid4(), email="c@example.com", role=UserRole.CUSTOMER)
    db_session.add(customer)
    db_session.commit()
    refused = lead(api, 400, owner_id=str(customer.id))
    assert "active administrator" in refused["detail"]
    lead(api, 400, owner_id=str(uuid.uuid4()))
    lead(api, 400, property_id=str(uuid.uuid4()))
    made = lead(api)
    # Clearing both names on update is refused too.
    response = api.patch(f"{CRM}/leads/{made['id']}", json={"company_name": None})
    assert response.status_code == 400


def test_stages_move_forward_one_at_a_time(api: TestClient) -> None:
    made = lead(api)
    url = f"{CRM}/leads/{made['id']}/stage"
    skipped = api.post(url, json={"stage": "SPACE_SELECTED"})
    assert skipped.status_code == 409 and "Lead Reviewed" in skipped.text
    assert api.post(url, json={"stage": "LEAD_CREATED"}).status_code == 409  # already there
    reviewed = api.post(url, json={"stage": "LEAD_REVIEWED"})
    assert reviewed.status_code == 200 and reviewed.json()["next_stage"] == "SPACE_SELECTED"
    assert api.post(url, json={"stage": "LEAD_CREATED"}).status_code == 409  # never backwards
    done = api.post(url, json={"stage": "SPACE_SELECTED"}).json()
    assert (done["stage"], done["next_stage"]) == ("SPACE_SELECTED", None)
    for later in ("CONTRACT_PREPARED", "CONTRACT_COMPLETED", "TENANT_ACTIVATED", "LOST"):
        assert api.post(url, json={"stage": later}).status_code == 422


def test_duplicates_are_warned_about_across_leads_and_tenants_never_merged(
    api: TestClient, db_session: Session
) -> None:
    db_session.add(
        Tenant(
            name="Acme Holdings",
            company_name="ACME  ltd.",
            email="ops@acme.in",
            phone="+91 98765 43210",
        )
    )
    db_session.commit()
    first = lead(api, contact_email="ops@acme.in")
    second = lead(
        api, company_name="Acme Ltd", contact_phone="098765-43210", contact_email="x@other.in"
    )
    # Both were created: a possible duplicate never stops a lead.
    assert db_session.scalar(select(func.count()).select_from(Lead)) == 2
    found = api.get(f"{CRM}/leads/{second['id']}/duplicates").json()
    by = {(m["kind"], m["name"]): set(m["matched_on"]) for m in found}
    assert by[("TENANT", "Acme Holdings")] == {"phone", "company"}
    assert by[("LEAD", "Acme Ltd")] == {"company"}  # the first lead, by company only
    assert all(m["id"] != second["id"] for m in found)  # never itself
    # Before saving: the check reads only, and names are never matched on their own.
    check = api.post(
        f"{CRM}/leads/check-duplicates", json={"contact_email": " OPS@acme.IN "}
    ).json()
    assert {(m["kind"], m["id"]) for m in check} >= {("LEAD", first["id"])}
    assert api.post(f"{CRM}/leads/check-duplicates", json={"company_name": "Zeta"}).json() == []
    # Nothing was merged or changed.
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    assert api.get(f"{CRM}/leads/{first['id']}").json()["stage"] == "LEAD_CREATED"


def test_interests_link_properties_and_spaces_without_holding_them(
    api: TestClient, hub: dict
) -> None:
    made = lead(api)
    url = f"{CRM}/leads/{made['id']}/interests"
    pid = hub["property"]["id"]
    whole = api.post(url, json={"property_id": pid})
    cabin = api.post(
        url, json={"property_id": pid, "space_id": hub["cabin"]["id"], "note": "Window side"}
    )
    assert whole.status_code == 201 and cabin.status_code == 201
    assert cabin.json()["space_name"] == "Cabin 1"
    assert api.post(url, json={"property_id": pid}).status_code == 409  # listed already
    other = create_property(api, name="Other Park")
    wrong = api.post(url, json={"property_id": other["id"], "space_id": hub["cabin"]["id"]})
    assert wrong.status_code == 400 and "belong to the chosen property" in wrong.text
    assert len(api.get(url).json()) == 2
    assert api.delete(f"{url}/{whole.json()['id']}").status_code == 204
    assert [i["space_name"] for i in api.get(url).json()] == ["Cabin 1"]


def test_notes_and_tasks_and_due_overdue(api: TestClient, admin_user: AppUser) -> None:
    made = lead(api)
    notes = f"{CRM}/leads/{made['id']}/notes"
    note = api.post(notes, json={"body": "Called; wants a site visit."}).json()
    assert (
        api.patch(f"{notes}/{note['id']}", json={"body": "Visit on Friday."}).json()["body"]
        == "Visit on Friday."
    )
    assert api.post(notes, json={"body": ""}).status_code == 422
    assert api.delete(f"{notes}/{note['id']}").status_code == 204 and api.get(notes).json() == []

    tasks = f"{CRM}/leads/{made['id']}/tasks"
    today = business_today()
    late = api.post(
        tasks,
        json={"title": "Send floor plan", "due_date": (today - timedelta(days=2)).isoformat()},
    ).json()
    now = api.post(
        tasks,
        json={"title": "Call back", "due_date": today.isoformat(), "owner_id": str(admin_user.id)},
    ).json()
    soon = api.post(
        tasks, json={"title": "Visit", "due_date": (today + timedelta(days=3)).isoformat()}
    ).json()
    assert (late["state"], now["state"], soon["state"]) == ("OVERDUE", "DUE_TODAY", "UPCOMING")
    assert api.get(f"{CRM}/leads/{made['id']}").json()["overdue_tasks"] == 1

    def titles(**params: str) -> list[str]:
        return [t["title"] for t in api.get(f"{CRM}/tasks", params=params).json()["items"]]

    assert titles(due="overdue") == ["Send floor plan"]
    assert titles(due="today") == ["Call back"]
    assert titles(due="upcoming") == ["Visit"]
    assert titles() == ["Send floor plan", "Call back", "Visit"]  # open, oldest due first
    assert titles(owner_id=str(admin_user.id)) == ["Call back"]
    finished = api.patch(f"{tasks}/{late['id']}", json={"done": True}).json()
    assert finished["state"] == "DONE" and finished["completed_at"]
    assert titles(due="overdue") == [] and titles(due="done") == ["Send floor plan"]
    assert api.patch(f"{tasks}/{late['id']}", json={"done": False}).json()["completed_at"] is None
    assert api.patch(f"{tasks}/{soon['id']}", json={"due_date": None}).status_code == 422
    assert api.delete(f"{tasks}/{soon['id']}").status_code == 204
    assert api.get(f"{CRM}/tasks", params={"due": "soon"}).status_code == 422


def test_list_pagination_search_and_filters(
    api: TestClient, hub: dict, admin_user: AppUser
) -> None:
    for n in range(5):
        lead(api, company_name=f"Company {n}", source="EXCEL" if n % 2 else "MANUAL")
    tagged = lead(
        api,
        company_name="Zeta Works",
        property_id=hub["property"]["id"],
        owner_id=str(admin_user.id),
    )
    pages = [api.get(f"{CRM}/leads", params={"page": p, "page_size": 4}).json() for p in (1, 2)]
    assert [p["total"] for p in pages] == [6, 6]
    ids = [i["id"] for p in pages for i in p["items"]]
    assert len(ids) == len(set(ids)) == 6 and ids[0] == tagged["id"]  # newest first
    assert api.get(f"{CRM}/leads", params={"source": "EXCEL"}).json()["total"] == 2
    assert api.get(f"{CRM}/leads", params={"search": "zeta"}).json()["total"] == 1
    assert api.get(f"{CRM}/leads", params={"owner_id": str(admin_user.id)}).json()["total"] == 1
    assert (
        api.get(f"{CRM}/leads", params={"property_id": hub["property"]["id"]}).json()["total"] == 1
    )
    assert api.get(f"{CRM}/leads", params={"stage": "LEAD_REVIEWED"}).json()["total"] == 0
    today = business_today().isoformat()
    assert (
        api.get(f"{CRM}/leads", params={"created_from": today, "created_to": today}).json()["total"]
        == 6
    )
    assert (
        api.get(
            f"{CRM}/leads", params={"created_from": today, "created_to": "2020-01-01"}
        ).status_code
        == 400
    )
    assert [o["email"] for o in api.get(f"{CRM}/owners").json()] == [admin_user.email]


def test_every_step_is_in_the_activity_history(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    made = lead(api, property_id=hub["property"]["id"])
    base = f"{CRM}/leads/{made['id']}"
    api.patch(base, json={"contact_name": "Priya"})
    api.post(f"{base}/stage", json={"stage": "LEAD_REVIEWED"})
    api.post(f"{base}/notes", json={"body": "Hello"})
    task = api.post(
        f"{base}/tasks", json={"title": "Call", "due_date": business_today().isoformat()}
    ).json()
    api.patch(f"{base}/tasks/{task['id']}", json={"done": True})
    api.post(f"{base}/interests", json={"property_id": hub["property"]["id"]})
    actions = db_session.scalars(
        select(ActivityLog.action).where(
            ActivityLog.entity_type == "leads", ActivityLog.entity_id == uuid.UUID(made["id"])
        )
    ).all()
    assert set(actions) == {
        "created",
        "updated",
        "stage_changed",
        "note_added",
        "task_added",
        "task_completed",
        "interest_added",
    }
    history = api.get(
        f"{V1}/admin/activity", params={"entity_type": "leads", "entity_id": made["id"]}
    ).json()
    assert history["total"] == 7
    labels = {i["summary"] for i in history["items"]}
    assert "Lead: stage changed" in labels and "Lead: follow-up completed" in labels
    assert all(i["record"]["state"] == "EXISTS" for i in history["items"])
