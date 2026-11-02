"""CRM core (Phase 3A): leads, interests, notes, follow-up tasks, duplicate warnings.

Admin only. Nothing here converts a lead to a tenant, holds inventory or sends messages.
"""

import json
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, Query, Response, UploadFile

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED, _error
from app.core.errors import BadRequestError
from app.models.enums import LeadImportFileType, LeadSource, LeadStage
from app.schemas.common import Page
from app.schemas.crm import (
    DuplicateCheck,
    DuplicateMatch,
    InterestCreate,
    InterestRead,
    LeadCreate,
    LeadRead,
    LeadUpdate,
    NoteCreate,
    NoteRead,
    OwnerRead,
    StageChange,
    TaskCreate,
    TaskRead,
    TaskUpdate,
)
from app.schemas.lead_import import ImportBatchRead, ImportBatchSummary, ImportPreview
from app.services.crm import CrmService
from app.services.lead_import_files import MAX_FILE_BYTES, template
from app.services.lead_imports import LeadImportService

router = APIRouter(prefix="/crm", tags=["CRM"], responses=PROTECTED)
BAD = {400: _error("The request breaks a CRM rule (owner, property, space or values).")}
STAGE = {409: _error("Stages move forward one at a time; later stages come from contracts.")}


@router.get("/leads", response_model=Page[LeadRead], summary="List leads")
def list_leads(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    stage: LeadStage | None = None,
    source: LeadSource | None = None,
    owner_id: uuid.UUID | None = None,
    property_id: uuid.UUID | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> Page[LeadRead]:
    """Newest first. `search` matches reference, company, contact name, email and phone.
    Dates are calendar days in the business time zone, both included."""
    items, total = CrmService(session, admin).leads(
        query,
        stage=stage,
        source=source,
        owner_id=owner_id,
        property_id=property_id,
        created_from=created_from,
        created_to=created_to,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "/leads", response_model=LeadRead, status_code=201, responses=BAD, summary="Create a lead"
)
def create_lead(admin: CurrentAdmin, session: DbSession, body: LeadCreate) -> LeadRead:
    """Starts at Lead Created. Possible duplicates do not stop it: check them with
    /leads/check-duplicates before, or /leads/{id}/duplicates after."""
    return CrmService(session, admin).create(body.model_dump())


@router.post(
    "/leads/check-duplicates",
    response_model=list[DuplicateMatch],
    summary="Possible duplicates for given details",
)
def check_duplicates(
    admin: CurrentAdmin, session: DbSession, body: DuplicateCheck
) -> list[DuplicateMatch]:
    """Leads and tenants with the same normalised email, phone or company. Warning only:
    nothing is merged, refused or changed."""
    return CrmService(session, admin).check_duplicates(**body.model_dump())


@router.get("/owners", response_model=list[OwnerRead], summary="Administrators who can own leads")
def owners(admin: CurrentAdmin, session: DbSession) -> list[OwnerRead]:
    return CrmService(session, admin).owners()


@router.get("/leads/{lead_id}", response_model=LeadRead, responses=NOT_FOUND, summary="A lead")
def get_lead(admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID) -> LeadRead:
    return CrmService(session, admin).get(lead_id)


@router.patch(
    "/leads/{lead_id}",
    response_model=LeadRead,
    responses={**NOT_FOUND, **BAD},
    summary="Update a lead",
)
def update_lead(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, body: LeadUpdate
) -> LeadRead:
    """The source never changes; the stage has its own endpoint."""
    return CrmService(session, admin).update(lead_id, body.model_dump(exclude_unset=True))


@router.post(
    "/leads/{lead_id}/stage",
    response_model=LeadRead,
    responses={**NOT_FOUND, **STAGE},
    summary="Move a lead to its next stage",
)
def change_stage(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, body: StageChange
) -> LeadRead:
    return CrmService(session, admin).change_stage(lead_id, body.stage)


@router.get(
    "/leads/{lead_id}/duplicates",
    response_model=list[DuplicateMatch],
    responses=NOT_FOUND,
    summary="Possible duplicates of a lead",
)
def lead_duplicates(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID
) -> list[DuplicateMatch]:
    return CrmService(session, admin).duplicates_of(lead_id)


@router.get(
    "/leads/{lead_id}/interests",
    response_model=list[InterestRead],
    responses=NOT_FOUND,
    summary="Properties and spaces of interest",
)
def list_interests(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID
) -> list[InterestRead]:
    return CrmService(session, admin).interests(lead_id)


@router.post(
    "/leads/{lead_id}/interests",
    response_model=InterestRead,
    status_code=201,
    responses={**NOT_FOUND, **BAD, **CONFLICT},
    summary="Add a property or space of interest",
)
def add_interest(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, body: InterestCreate
) -> InterestRead:
    """Records interest only: no booking, contract or hold."""
    return CrmService(session, admin).add_interest(lead_id, body.model_dump())


@router.delete(
    "/leads/{lead_id}/interests/{interest_id}",
    status_code=204,
    responses=NOT_FOUND,
    summary="Remove an interest",
)
def remove_interest(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, interest_id: uuid.UUID
) -> None:
    CrmService(session, admin).remove_interest(lead_id, interest_id)


@router.get(
    "/leads/{lead_id}/notes", response_model=list[NoteRead], responses=NOT_FOUND, summary="Notes"
)
def list_notes(admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID) -> list[NoteRead]:
    return CrmService(session, admin).notes(lead_id)


@router.post(
    "/leads/{lead_id}/notes",
    response_model=NoteRead,
    status_code=201,
    responses=NOT_FOUND,
    summary="Add a note",
)
def add_note(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, body: NoteCreate
) -> NoteRead:
    return CrmService(session, admin).add_note(lead_id, body.body)


@router.patch(
    "/leads/{lead_id}/notes/{note_id}",
    response_model=NoteRead,
    responses=NOT_FOUND,
    summary="Edit a note",
)
def update_note(
    admin: CurrentAdmin,
    session: DbSession,
    lead_id: uuid.UUID,
    note_id: uuid.UUID,
    body: NoteCreate,
) -> NoteRead:
    return CrmService(session, admin).update_note(lead_id, note_id, body.body)


@router.delete(
    "/leads/{lead_id}/notes/{note_id}",
    status_code=204,
    responses=NOT_FOUND,
    summary="Delete a note",
)
def delete_note(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    CrmService(session, admin).delete_note(lead_id, note_id)


@router.get(
    "/leads/{lead_id}/tasks",
    response_model=Page[TaskRead],
    responses=NOT_FOUND,
    summary="A lead's follow-ups",
)
def list_lead_tasks(
    admin: CurrentAdmin,
    session: DbSession,
    lead_id: uuid.UUID,
    query: ListParams,
    due: Annotated[str, Query(pattern="^(overdue|today|upcoming|open|done|all)$")] = "all",
) -> Page[TaskRead]:
    items, total = CrmService(session, admin).tasks(query, lead_id=lead_id, due=due)
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "/leads/{lead_id}/tasks",
    response_model=TaskRead,
    status_code=201,
    responses={**NOT_FOUND, **BAD},
    summary="Add a follow-up",
)
def add_task(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, body: TaskCreate
) -> TaskRead:
    return CrmService(session, admin).add_task(lead_id, body.model_dump())


@router.patch(
    "/leads/{lead_id}/tasks/{task_id}",
    response_model=TaskRead,
    responses={**NOT_FOUND, **BAD},
    summary="Update, complete or reopen a follow-up",
)
def update_task(
    admin: CurrentAdmin,
    session: DbSession,
    lead_id: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskUpdate,
) -> TaskRead:
    return CrmService(session, admin).update_task(
        lead_id, task_id, body.model_dump(exclude_unset=True)
    )


@router.delete(
    "/leads/{lead_id}/tasks/{task_id}",
    status_code=204,
    responses=NOT_FOUND,
    summary="Delete a follow-up",
)
def delete_task(
    admin: CurrentAdmin, session: DbSession, lead_id: uuid.UUID, task_id: uuid.UUID
) -> None:
    CrmService(session, admin).delete_task(lead_id, task_id)


@router.get("/tasks", response_model=Page[TaskRead], summary="Follow-ups across all leads")
def list_tasks(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    due: Annotated[str, Query(pattern="^(overdue|today|upcoming|open|done|all)$")] = "open",
    owner_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
) -> Page[TaskRead]:
    """Due dates are compared with today's date in the business time zone. Open tasks are
    listed by due date, oldest first. Nothing is sent: these are in-app reminders only."""
    items, total = CrmService(session, admin).tasks(
        query, lead_id=lead_id, due=due, owner_id=owner_id
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


# --- spreadsheet import (Phase 3B-1) ----------------------------------------------------

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_IMPORT_ERRORS = {
    400: _error("The file or its mapping cannot be used."),
    413: _error("The file is larger than 5 MB."),
    415: _error("Not an .xlsx or .csv file, or a workbook with macros."),
}


def _read_upload(file: UploadFile) -> bytes:
    # One byte more than allowed, so an oversized file is recognised without reading it all.
    return file.file.read(MAX_FILE_BYTES + 1)


def _json_list(text: str | None, name: str) -> list[object] | None:
    if text is None or not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise BadRequestError(f"{name} must be a JSON list.") from exc
    if not isinstance(value, list):
        raise BadRequestError(f"{name} must be a JSON list.")
    return value


def _mapping(text: str | None) -> list[str | None] | None:
    value = _json_list(text, "mapping")
    if value is not None and not all(v is None or isinstance(v, str) for v in value):
        raise BadRequestError("mapping must list a field name or null for each column.")
    return value  # type: ignore[return-value]


@router.get(
    "/imports/template",
    response_class=Response,
    responses={200: {"content": {_XLSX: {}, "text/csv": {}}}},
    summary="Blank lead import template",
)
def import_template(
    admin: CurrentAdmin, format: LeadImportFileType = LeadImportFileType.XLSX
) -> Response:
    """The header row of the import template, as .xlsx (default) or CSV UTF-8."""
    xlsx = format == LeadImportFileType.XLSX
    return Response(
        content=template(format),
        media_type=_XLSX if xlsx else "text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; "
            f'filename="lead-import-template.{"xlsx" if xlsx else "csv"}"'
        },
    )


@router.post(
    "/imports/preview",
    response_model=ImportPreview,
    responses=_IMPORT_ERRORS,
    summary="Read and check a lead spreadsheet without saving anything",
)
def preview_import(
    admin: CurrentAdmin,
    session: DbSession,
    file: UploadFile,
    mapping: Annotated[
        str | None,
        Form(description="JSON list: a field name or null for each column. Omit to match headers."),
    ] = None,
) -> ImportPreview:
    """Multipart form. .xlsx or .csv, at most 5 MB and 2,000 data rows; the first sheet is
    read. Every row is checked like a lead entered by hand and compared with existing
    leads and tenants (email, phone, company; never names alone) and with the other rows.
    Nothing is saved: no lead and no import record."""
    return LeadImportService(session, admin).preview(
        _read_upload(file), file.filename, _mapping(mapping)
    )


@router.post(
    "/imports",
    response_model=ImportBatchRead,
    status_code=201,
    responses={**_IMPORT_ERRORS, **CONFLICT},
    summary="Import a previewed lead spreadsheet",
)
def run_import(
    admin: CurrentAdmin,
    session: DbSession,
    file: UploadFile,
    fingerprint: Annotated[
        str, Form(pattern="^[0-9a-f]{64}$", description="From the preview of this file.")
    ],
    mapping: Annotated[str | None, Form(description="As used for the preview.")] = None,
    skip_rows: Annotated[
        str | None, Form(description="JSON list of row numbers the admin chose to skip.")
    ] = None,
) -> ImportBatchRead:
    """The same file again: every row is checked again, then the valid rows not skipped
    are created in one transaction (all or nothing) with source EXCEL at Lead Created.
    Rows with errors are left out and reported. Possible duplicates are imported with a
    warning unless skipped; nothing is merged. The file itself is not kept."""
    skips = _json_list(skip_rows, "skip_rows") or []
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in skips):
        raise BadRequestError("skip_rows must list row numbers.")
    return LeadImportService(session, admin).run(
        _read_upload(file),
        file.filename,
        _mapping(mapping),
        set(skips),
        fingerprint,  # type: ignore[arg-type]
    )


@router.get("/imports", response_model=Page[ImportBatchSummary], summary="Lead import history")
def list_imports(
    admin: CurrentAdmin, session: DbSession, query: ListParams
) -> Page[ImportBatchSummary]:
    """Newest first. Search matches the file name."""
    items, total = LeadImportService(session, admin).batches(query)
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/imports/{import_id}",
    response_model=ImportBatchRead,
    responses=NOT_FOUND,
    summary="One import and each row's outcome",
)
def get_import(admin: CurrentAdmin, session: DbSession, import_id: uuid.UUID) -> ImportBatchRead:
    return LeadImportService(session, admin).get(import_id)


@router.get(
    "/imports/{import_id}/report",
    response_class=Response,
    responses={**NOT_FOUND, 200: {"content": {"text/csv": {}}}},
    summary="Each row's outcome as CSV",
)
def import_report(admin: CurrentAdmin, session: DbSession, import_id: uuid.UUID) -> Response:
    """For Excel: values starting with =, +, - or @ are prefixed with ' so they are shown
    as text and never run as formulas."""
    name, content = LeadImportService(session, admin).report_csv(import_id)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )
