# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Meta Ads lead intake API (Phase 3B-2, polling). Needs the migrated test database.
Meta is never called: a stand-in client returns recorded-style payloads."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.meta_leads import get_meta_client
from app.main import app
from app.models import ActivityLog, Lead, LeadNote, MetaLeadEvent
from app.services.meta_leads import MetaClientError
from tests.test_client_access import new_tenant
from tests.test_client_api import priya  # noqa: F401
from tests.test_resources import V1

META = f"{V1}/admin/crm/meta"


def lead(leadgen_id: str, created: str, **answers: str) -> dict:
    return {
        "id": leadgen_id,
        "created_time": created,
        "ad_id": "ad-1",
        "campaign_id": "camp-1",
        "field_data": [{"name": k, "values": [v]} for k, v in answers.items()],
    }


class StubMeta:
    def __init__(self) -> None:
        self.leads: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, datetime | None]] = []
        self.broken: set[str] = set()

    def fetch_leads(self, form_id: str, since: datetime | None) -> list[dict]:
        self.calls.append((form_id, since))
        if form_id in self.broken:
            raise MetaClientError("Meta refused the request (HTTP 400, OAuthException code 190).")
        return list(self.leads.get(form_id, []))


@pytest.fixture
def meta() -> Iterator[StubMeta]:
    stand_in = StubMeta()
    app.dependency_overrides[get_meta_client] = lambda: stand_in
    yield stand_in
    app.dependency_overrides.pop(get_meta_client, None)


def add_form(api: TestClient, form_id: str = "555", name: str = "Offices form") -> dict:
    made = api.post(f"{META}/forms", json={"form_id": form_id, "name": name})
    assert made.status_code == 201, made.text
    return made.json()


def enable(api: TestClient, form: dict, **mapping: str | None) -> dict:
    body = {"enabled": True, "field_mapping": {**form["field_mapping"], **mapping}}
    done = api.patch(f"{META}/forms/{form['id']}", json=body)
    assert done.status_code == 200, done.text
    return done.json()


def test_only_admins_reach_meta(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        assert who.get(f"{META}/status").status_code == status
        assert who.post(f"{META}/sync").status_code == status
        assert who.get(f"{META}/events").status_code == status
    assert priya.get(f"{V1}/client/crm/meta/status").status_code == 404


def test_status_names_missing_settings_never_values(api: TestClient) -> None:
    status = api.get(f"{META}/status").json()
    assert status["configured"] is False
    assert status["missing_settings"] == [
        "META_GRAPH_API_VERSION",
        "META_PAGE_ACCESS_TOKEN",
        "META_APP_SECRET",
    ]


def test_sync_without_configuration_is_refused(api: TestClient) -> None:
    app.dependency_overrides[get_meta_client] = lambda: None
    try:
        refused = api.post(f"{META}/sync")
    finally:
        app.dependency_overrides.pop(get_meta_client, None)
    assert refused.status_code == 503
    assert refused.json()["detail"] == "Meta Ads is not configured on the server."


def test_forms_start_disabled_with_meta_standard_questions_mapped(api: TestClient) -> None:
    form = add_form(api)
    assert form["enabled"] is False
    assert form["field_mapping"] == {
        "full_name": "contact_name",
        "email": "contact_email",
        "phone_number": "contact_phone",
        "company_name": "company_name",
    }
    assert api.post(f"{META}/forms", json={"form_id": "555", "name": "Again"}).status_code == 409
    owner = api.patch(f"{META}/forms/{form['id']}", json={"field_mapping": {"rep": "owner_id"}})
    assert owner.status_code == 400 and "owner cannot be mapped" in owner.json()["detail"]


def test_sync_imports_each_meta_lead_once_with_excel_rules(
    api: TestClient, meta: StubMeta, db_session: Session
) -> None:
    new_tenant(api)  # ops@acme.example, to be warned about
    form = enable(api, add_form(api), desks="capacity")
    meta.leads["555"] = [
        lead(
            "L1",
            "2026-10-31T10:00:00+0000",
            full_name="Priya Nair",
            email="priya@acme.in",
            desks="4",
            budget_note="Around 50k",
        ),
        lead("L2", "2026-10-31T11:00:00+0000", company_name="Acme", email="ops@acme.example"),
        lead("L3", "2026-10-31T12:00:00+0000", full_name="Ravi", email="not-an-email"),
    ]
    first = api.post(f"{META}/sync").json()
    assert (first["received"], first["imported"], first["failed"]) == (3, 2, 1)
    leads = {x.contact_email: x for x in db_session.scalars(select(Lead))}
    priya_lead = leads["priya@acme.in"]
    assert (priya_lead.source, priya_lead.stage, priya_lead.owner_id) == (
        "META_ADS",
        "LEAD_CREATED",
        None,
    )
    assert priya_lead.capacity == 4
    notes = [
        n.body
        for n in db_session.scalars(select(LeadNote).where(LeadNote.lead_id == priya_lead.id))
    ]
    assert notes == ["Answers from the Meta lead form:\nbudget_note: Around 50k"]

    events = {e["leadgen_id"]: e for e in api.get(f"{META}/events").json()["items"]}
    assert events["L1"]["status"] == "IMPORTED" and events["L1"]["ad_id"] == "ad-1"
    assert events["L2"]["status"] == "IMPORTED"
    assert [d["kind"] for d in events["L2"]["duplicates"]] == ["TENANT"]  # warned, not merged
    # The reason names the Meta question, the column an admin maps.
    assert events["L3"]["status"] == "FAILED"
    assert events["L3"]["last_error"].startswith("email: value is not a valid email address")

    # Fetching the same leads again (a retried sync) creates nothing new.
    again = api.post(f"{META}/sync").json()
    assert again["received"] == 0 and again["forms"][0]["already_known"] == 3
    assert db_session.scalar(select(func.count()).select_from(Lead)) == 2
    assert db_session.scalar(select(func.count()).select_from(MetaLeadEvent)) == 3
    # The next sync asks only for leads newer than the newest seen.
    assert meta.calls[-1][1] == datetime(2026, 10, 31, 12, 0, tzinfo=UTC)  # same instant

    history = db_session.scalars(
        select(ActivityLog.message).where(ActivityLog.entity_type == "leads")
    ).all()
    assert any("from Meta Ads form Offices form (Meta lead L1)" in m for m in history)
    assert form["id"]


def test_a_failed_lead_can_be_reprocessed_after_the_mapping_is_fixed(
    api: TestClient, meta: StubMeta, db_session: Session
) -> None:
    form = enable(api, add_form(api), desks="capacity")
    meta.leads["555"] = [lead("L9", "2026-10-31T10:00:00+0000", full_name="Asha", desks="4 to 6")]
    assert api.post(f"{META}/sync").json()["failed"] == 1
    failed = api.get(f"{META}/events", params={"status": "FAILED"}).json()["items"][0]
    assert failed["attempts"] == 1 and "whole number" in failed["last_error"]
    enable(api, form, desks=None)  # keep the answer in the note instead
    done = api.post(f"{META}/events/{failed['id']}/reprocess").json()
    assert (done["status"], done["attempts"]) == ("IMPORTED", 2)
    assert done["lead_reference"].startswith("LD-")
    assert api.post(f"{META}/events/{failed['id']}/reprocess").status_code == 409  # never twice
    assert db_session.scalar(select(func.count()).select_from(Lead)) == 1


def test_a_form_meta_refuses_is_reported_and_others_continue(
    api: TestClient, meta: StubMeta
) -> None:
    enable(api, add_form(api, "111", "A form"))
    enable(api, add_form(api, "222", "B form"))
    meta.broken.add("111")
    meta.leads["222"] = [lead("B1", "2026-10-31T10:00:00+0000", full_name="Zed")]
    result = api.post(f"{META}/sync").json()
    by_form = {f["form_id"]: f for f in result["forms"]}
    assert (
        by_form["111"]["error"] == "Meta refused the request (HTTP 400, OAuthException code 190)."
    )
    assert by_form["222"]["imported"] == 1
    forms = {f["form_id"]: f for f in api.get(f"{META}/forms").json()}
    assert forms["111"]["last_sync_error"] and forms["222"]["last_sync_error"] is None
    assert forms["222"]["seen_fields"] == ["full_name"]


def test_disabled_forms_are_not_fetched(api: TestClient, meta: StubMeta) -> None:
    add_form(api)
    assert api.post(f"{META}/sync").json()["forms"] == []
    assert meta.calls == []
