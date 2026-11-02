# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Lead spreadsheet import API (Phase 3B-1). Needs the migrated test database (like every
API test here). Sample property "Sigma Hub" comes from test_client_api."""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ActivityLog, AppUser, Lead, LeadImportBatch, LeadNote
from app.services.crm import CrmService
from app.services.lead_import_files import fingerprint
from tests.test_client_access import new_tenant
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_resources import V1

IMPORTS = f"{V1}/admin/crm/imports"
HEADER = (
    "Company,Contact name,Email,Phone,Preferred property,Budget (₹),Budget per,Owner email,"
    "Notes,Source col\r\n"
)


def sheet(*rows: str) -> bytes:
    return (HEADER + "".join(r + "\r\n" for r in rows)).encode("utf-8")


def preview(
    api: TestClient, data: bytes, mapping: list | None = None, name: str = "leads.csv"
) -> dict:
    form = {"mapping": json.dumps(mapping)} if mapping is not None else {}
    response = api.post(f"{IMPORTS}/preview", files={"file": (name, data, "text/csv")}, data=form)
    assert response.status_code == 200, response.text
    return response.json()


def run(
    api: TestClient,
    data: bytes,
    *,
    skip: list[int] = (),  # type: ignore[assignment]
    expected: int = 201,
    digest: str | None = None,
    name: str = "leads.csv",
) -> dict:
    response = api.post(
        IMPORTS,
        files={"file": (name, data, "text/csv")},
        data={"fingerprint": digest or fingerprint(data), "skip_rows": json.dumps(list(skip))},
    )
    assert response.status_code == expected, response.text
    return response.json()


def counts(db: Session) -> tuple[int, int, int, int]:
    return (
        db.scalar(select(func.count()).select_from(Lead)),
        db.scalar(select(func.count()).select_from(LeadImportBatch)),
        db.scalar(select(func.count()).select_from(LeadNote)),
        db.scalar(select(func.count()).select_from(ActivityLog)),
    )


FILE = sheet(
    'Acme Ltd,Priya Nair,priya@acme.in,98765 43210,sigma hub,"₹50,000",Monthly,'
    "admin@example.com,Wants a site visit,x",
    "Beta Works,,ops@acme.example,,,,,,,",  # same email as an existing tenant
    ",,,,,,,,,",  # blank: ignored
    "Gamma,,gamma@g.in,,Nowhere,,,,,",  # unknown property: error
    "ACME LTD.,Ravi,,,,,,,,",  # same company as row 2 of this file
    "Delta,,delta@d.in,,,1000,,,,",  # budget without its period: error
)


def test_only_admins_reach_the_import(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        files = {"file": ("leads.csv", FILE, "text/csv")}
        assert who.post(f"{IMPORTS}/preview", files=files).status_code == status
        assert who.post(IMPORTS, files=files, data={"fingerprint": "0" * 64}).status_code == status
        assert who.get(IMPORTS).status_code == status
        assert who.get(f"{IMPORTS}/template").status_code == status
        assert who.get(f"{IMPORTS}/00000000-0000-0000-0000-000000000000").status_code == status
        assert (
            who.get(f"{IMPORTS}/00000000-0000-0000-0000-000000000000/report").status_code == status
        )
    # Nothing of the import is reachable through the client prefix.
    assert priya.post(f"{V1}/client/crm/imports/preview").status_code == 404


def test_preview_checks_every_row_and_saves_nothing(
    api: TestClient, hub: dict, admin_user: AppUser, db_session: Session
) -> None:
    new_tenant(api)  # ops@acme.example
    before = counts(db_session)
    shown = preview(api, FILE)
    assert counts(db_session) == before  # no lead, no import record, no history entry
    assert shown["fingerprint"] == fingerprint(FILE)
    assert shown["file_type"] == "CSV" and shown["previous_imports"] == []
    assert shown["unknown_columns"] == ["Source col"]
    assert [c["field"] for c in shown["columns"]][-1] is None
    assert (
        shown["total_rows"],
        shown["ready_count"],
        shown["error_count"],
        shown["duplicate_count"],
    ) == (
        5,
        0,
        2,
        3,
    )
    by_row = {r["row"]: r for r in shown["rows"]}
    assert sorted(by_row) == [2, 3, 5, 6, 7]  # the spreadsheet's own row numbers
    assert by_row[2]["status"] == "DUPLICATE" and by_row[2]["repeated_in_file"] == [6]
    assert by_row[3]["status"] == "DUPLICATE"
    assert [(d["kind"], d["matched_on"]) for d in by_row[3]["duplicates"]] == [
        ("TENANT", ["email"])
    ]
    assert by_row[5]["errors"] == [
        {
            "column": "Preferred property",
            "field": "property_id",
            "message": "No live property has this name.",
        }
    ]
    assert by_row[6]["status"] == "DUPLICATE" and by_row[6]["repeated_in_file"] == [2]
    assert by_row[7]["errors"][0]["column"] == "Budget (₹) / Budget per"
    assert by_row[2]["values"]["budget_amount"] == "₹50,000"


def test_import_creates_only_valid_rows_not_skipped(
    api: TestClient, hub: dict, admin_user: AppUser, db_session: Session
) -> None:
    new_tenant(api)
    done = run(api, FILE, skip=[3])  # skip the row matching a tenant; import the rest
    assert (
        done["total_rows"],
        done["imported_count"],
        done["skipped_count"],
        done["error_count"],
        done["duplicate_count"],
    ) == (5, 2, 1, 2, 3)
    assert done["created_by_email"] == "admin@example.com"
    outcomes = {r["row"]: r["outcome"] for r in done["rows"]}
    assert outcomes == {2: "IMPORTED", 3: "SKIPPED", 5: "ERROR", 6: "IMPORTED", 7: "ERROR"}

    leads = db_session.scalars(select(Lead).order_by(Lead.reference)).all()
    assert [lead.company_name for lead in leads] == ["Acme Ltd", "ACME LTD."]
    acme = leads[0]
    assert (acme.source, acme.stage, str(acme.import_batch_id)) == (
        "EXCEL",
        "LEAD_CREATED",
        done["id"],
    )
    assert str(acme.property_id) == hub["property"]["id"]
    assert acme.owner_id == admin_user.id
    assert (acme.budget_amount, acme.budget_period, acme.contact_phone) == (
        50000,
        "MONTHLY",
        "98765 43210",
    )
    assert [n.body for n in db_session.scalars(select(LeadNote))] == ["Wants a site visit"]
    imported = next(r for r in done["rows"] if r["row"] == 2)
    assert imported["lead_reference"] == acme.reference and imported["values"] == {}
    skipped = next(r for r in done["rows"] if r["row"] == 3)
    assert skipped["values"]["contact_email"] == "ops@acme.example"
    assert skipped["duplicates"][0]["kind"] == "TENANT"

    # History: each lead says it came from the import; the import is recorded once.
    messages = db_session.scalars(
        select(ActivityLog.message).where(ActivityLog.entity_type == "leads")
    ).all()
    assert any(
        f"{acme.reference} (Acme Ltd · Priya Nair) was created by import from leads.csv (row 2)"
        in m
        for m in messages
    )
    batch_log = db_session.scalars(
        select(ActivityLog).where(ActivityLog.entity_type == "lead_imports")
    ).one()
    assert batch_log.action == "imported"
    assert batch_log.message == "2 lead(s) imported from leads.csv: 1 skipped, 2 with errors."

    listed = api.get(IMPORTS).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == done["id"]
    assert "rows" not in listed["items"][0]
    detail = api.get(f"{IMPORTS}/{done['id']}").json()
    assert detail["rows"] == done["rows"]
    assert [c["field"] for c in detail["columns"]][:3] == [
        "company_name",
        "contact_name",
        "contact_email",
    ]
    lead_page = api.get(f"{V1}/admin/crm/leads/{acme.id}").json()
    assert lead_page["source"] == "EXCEL"

    # The same file again: warned about (fingerprint and duplicates), never refused.
    again = preview(api, FILE)
    assert [p["id"] for p in again["previous_imports"]] == [done["id"]]
    assert any(d["kind"] == "LEAD" for d in {r["row"]: r for r in again["rows"]}[2]["duplicates"])


def test_rows_are_checked_again_when_imported(
    api: TestClient, hub: dict, admin_user: AppUser, db_session: Session
) -> None:
    data = sheet("Omega,,omega@o.in,,,,,,,")
    assert preview(api, data)["rows"][0]["status"] == "READY"
    # Someone adds the same email by hand between the preview and the import.
    api.post(
        f"{V1}/admin/crm/leads", json={"company_name": "Omega Corp", "contact_email": "Omega@O.in"}
    )
    done = run(api, data)
    row = done["rows"][0]
    assert row["outcome"] == "IMPORTED" and row["duplicates"][0]["kind"] == "LEAD"
    assert done["duplicate_count"] == 1


def test_the_import_must_be_the_previewed_file(api: TestClient, db_session: Session) -> None:
    before = counts(db_session)
    other = sheet("Changed,,c@c.in,,,,,,,")
    refused = run(api, other, digest=fingerprint(FILE), expected=409)
    assert "not the file that was previewed" in refused["detail"]
    assert counts(db_session) == before


def test_a_failed_import_keeps_nothing(
    api: TestClient,
    hub: dict,
    admin_user: AppUser,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = counts(db_session)
    original = CrmService.insert
    calls = {"n": 0}

    def failing(self: CrmService, values: dict, **kw: object) -> Lead:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away")
        return original(self, values, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(CrmService, "insert", failing)
    failed = run(api, FILE, expected=500)  # the app answers with its generic error
    assert failed["code"] == "internal_error" and "database" not in failed["detail"]
    db_session.expire_all()
    assert counts(db_session) == before  # no lead, note, import record or history entry
    assert calls["n"] == 2  # tried once; never retried


def test_the_report_never_hands_excel_a_formula(
    api: TestClient, hub: dict, admin_user: AppUser
) -> None:
    data = sheet('"=HYPERLINK(""http://x"")",,not-an-email,+91 98,,,,,,')
    done = run(api, data)
    response = api.get(f"{IMPORTS}/{done['id']}/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="leads-results.csv"' in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][:5] == ["Row", "Outcome", "Lead", "Problems", "Possible duplicates"]
    assert rows[1][:2] == ["2", "Error"]
    assert '\'=HYPERLINK("http://x")' in rows[1]
    assert "'+91 98" in rows[1]
    assert all(not cell.startswith(("=", "+", "-", "@")) for row in rows for cell in row)


def test_template_downloads(api: TestClient) -> None:
    xlsx = api.get(f"{IMPORTS}/template")
    assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")
    assert 'filename="lead-import-template.xlsx"' in xlsx.headers["content-disposition"]
    csv_file = api.get(f"{IMPORTS}/template", params={"format": "CSV"})
    assert csv_file.content.decode("utf-8-sig").startswith("Company,Contact name,Email,Phone")


def test_bad_uploads_are_refused_with_reasons(api: TestClient, db_session: Session) -> None:
    def status(name: str, data: bytes, **form: str) -> tuple[int, str]:
        r = api.post(f"{IMPORTS}/preview", files={"file": (name, data, "text/csv")}, data=form)
        return r.status_code, r.json()["detail"]

    assert status("old.xls", b"x")[0] == 415
    assert status("macro.xlsm", b"x")[0] == 415
    assert status("big.csv", b"a" * (5 * 1024 * 1024 + 1))[0] == 413
    assert status("fake.xlsx", b"Company\nAcme\n") == (400, "This is not a valid .xlsx file.")
    assert status("leads.csv", FILE, mapping="{bad")[0] == 400
    assert status("leads.csv", FILE, mapping=json.dumps(["company_name"]))[0] == 400
    assert db_session.scalar(select(func.count()).select_from(LeadImportBatch)) == 0
