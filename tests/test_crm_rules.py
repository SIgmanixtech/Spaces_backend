"""CRM core rules that need no database (Phase 3A): match keys, stages, task states and
request validation. The API behaviour is in tests/test_crm.py."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.models.enums import LeadStage
from app.schemas.crm import LeadCreate, LeadUpdate, StageChange, TaskCreate
from app.services.crm import company_key, email_key, next_stage, phone_key, task_state


def test_email_keys_ignore_case_and_spaces() -> None:
    assert email_key("  Priya.Nair@Example.COM ") == "priya.nair@example.com"
    assert email_key(None) is None and email_key("   ") is None


@pytest.mark.parametrize(
    "raw", ["98765 43210", "+91 98765 43210", "098765-43210", "91 9876543210", "(+91) 98765.43210"]
)
def test_indian_numbers_written_differently_share_one_key(raw: str) -> None:
    assert phone_key(raw) == "919876543210"


def test_other_phone_keys() -> None:
    assert phone_key("+44 20 7946 0958") == "442079460958"  # kept as given, no 91 added
    assert phone_key("12345") is None  # too short to compare
    assert phone_key(None) is None and phone_key("no digits") is None


def test_company_keys_drop_case_punctuation_and_extra_spaces_only() -> None:
    assert company_key("  Acme   Ltd. ") == company_key("acme ltd") == "acme ltd"
    assert company_key("Acme Pvt. Ltd.") == "acme pvt ltd"
    # No suffix stripping and no name matching: these stay different.
    assert company_key("Acme Pvt Ltd") != company_key("Acme Ltd")
    assert company_key("...") is None and company_key(None) is None


def test_stages_move_forward_one_step_and_stop_at_space_selected() -> None:
    assert next_stage(LeadStage.LEAD_CREATED) == LeadStage.LEAD_REVIEWED
    assert next_stage(LeadStage.LEAD_REVIEWED) == LeadStage.SPACE_SELECTED
    assert next_stage(LeadStage.SPACE_SELECTED) is None  # later stages come from contracts
    with pytest.raises(ValidationError):
        StageChange(stage="CONTRACT_PREPARED")  # not a stage an admin can set
    with pytest.raises(ValidationError):
        StageChange(stage="LOST")  # Lost/Disqualified is not decided (C1)


def test_task_states_from_due_date_and_today() -> None:
    today = date(2026, 11, 2)
    assert task_state(False, date(2026, 11, 1), today) == "OVERDUE"
    assert task_state(False, today, today) == "DUE_TODAY"
    assert task_state(False, date(2026, 11, 3), today) == "UPCOMING"
    assert task_state(True, date(2026, 10, 1), today) == "DONE"  # done is never overdue


def test_a_lead_needs_a_name_and_a_whole_budget() -> None:
    assert LeadCreate(company_name="Acme").source == "MANUAL"
    assert (
        LeadCreate(contact_name="Priya").contact_name == "Priya"
    )  # a contact name alone is enough
    with pytest.raises(ValidationError, match="company name or a contact name"):
        LeadCreate(contact_email="a@example.com")
    with pytest.raises(ValidationError, match="budget amount together"):
        LeadCreate(company_name="Acme", budget_amount=50000)
    LeadCreate(company_name="Acme", budget_amount=50000, budget_period="MONTHLY")


@pytest.mark.parametrize(
    "bad",
    [
        {"contact_email": "not-an-email"},
        {"capacity": 0},
        {"area_sqft": -5},
        {"budget_amount": -1, "budget_period": "MONTHLY"},
        {"currency": "inr"},
        {"source": "WALK_IN"},
        {"stage": "LEAD_REVIEWED"},  # the stage is not set on create
        {"space_type": "CASTLE"},
    ],
)
def test_invalid_lead_values_are_refused(bad: dict) -> None:
    with pytest.raises(ValidationError):
        LeadCreate(company_name="Acme", **bad)


def test_the_source_can_be_corrected_but_the_stage_has_its_own_endpoint() -> None:
    # Approved 2 Nov 2026: any lead's source may be corrected (logged old -> new).
    assert LeadUpdate(source="EXCEL").model_dump(exclude_unset=True) == {"source": "EXCEL"}
    with pytest.raises(ValidationError):
        LeadUpdate(source=None)  # a lead always has a source
    with pytest.raises(ValidationError):
        LeadUpdate(source="WEBSITE")
    with pytest.raises(ValidationError):
        LeadUpdate(stage="SPACE_SELECTED")
    with pytest.raises(ValidationError):
        LeadUpdate(currency=None)


def test_tasks_need_a_title_and_due_date() -> None:
    TaskCreate(title="Call back", due_date=date(2026, 11, 5))
    with pytest.raises(ValidationError):
        TaskCreate(title="", due_date=date(2026, 11, 5))
    with pytest.raises(ValidationError):
        TaskCreate(title="Call back")
