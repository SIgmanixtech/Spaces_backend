"""Meta Ads lead intake (Phase 3B-2, polling). Admin only, under /admin/crm/meta.

No endpoint here is public: Meta is polled by the server (the `sync-meta-leads` command,
or "Sync now"), never called back. The page token and app secret are read from protected
settings and never returned.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import AppSettings, CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED, _error
from app.core.config import Settings, get_settings
from app.models.enums import MetaLeadStatus
from app.schemas.common import Page
from app.schemas.meta_lead import (
    MetaEventRead,
    MetaFieldOptions,
    MetaFormCreate,
    MetaFormRead,
    MetaFormUpdate,
    MetaStatus,
    MetaSyncResult,
)
from app.services.meta_leads import MetaClient, MetaLeadService, client_from_settings

router = APIRouter(prefix="/crm/meta", tags=["CRM"], responses=PROTECTED)


def get_meta_client(settings: Annotated[Settings, Depends(get_settings)]) -> MetaClient | None:
    """The Graph API client when configured; tests replace this dependency."""
    return client_from_settings(settings)


Meta = Annotated[MetaClient | None, Depends(get_meta_client)]


@router.get("/status", response_model=MetaStatus, summary="Whether Meta Ads import is set up")
def meta_status(admin: CurrentAdmin, session: DbSession, settings: AppSettings) -> MetaStatus:
    """Names of missing settings are listed; their values never are."""
    return MetaLeadService(session, admin, None).status(settings)


@router.get("/fields", response_model=MetaFieldOptions, summary="Lead fields a question can fill")
def meta_fields(admin: CurrentAdmin, session: DbSession) -> MetaFieldOptions:
    return MetaLeadService(session, admin, None).field_options()


@router.get("/forms", response_model=list[MetaFormRead], summary="Meta lead forms")
def list_forms(admin: CurrentAdmin, session: DbSession) -> list[MetaFormRead]:
    return MetaLeadService(session, admin, None).forms()


@router.post(
    "/forms",
    response_model=MetaFormRead,
    status_code=201,
    responses=CONFLICT,
    summary="Add a Meta lead form (not enabled until reviewed)",
)
def add_form(admin: CurrentAdmin, session: DbSession, body: MetaFormCreate) -> MetaFormRead:
    """Starts with Meta's standard questions mapped (full name, email, phone, company)."""
    return MetaLeadService(session, admin, None).add_form(body.form_id, body.name)


@router.patch(
    "/forms/{form_ref}",
    response_model=MetaFormRead,
    responses=NOT_FOUND,
    summary="Rename, enable or map a Meta lead form",
)
def update_form(
    admin: CurrentAdmin, session: DbSession, form_ref: uuid.UUID, body: MetaFormUpdate
) -> MetaFormRead:
    """Each lead field can take one question; the owner cannot be mapped (Meta leads stay
    unassigned). Questions left unmapped go into the lead's first note."""
    return MetaLeadService(session, admin, None).update_form(
        form_ref, body.model_dump(exclude_unset=True)
    )


@router.post(
    "/sync",
    response_model=MetaSyncResult,
    responses={503: _error("Meta Ads is not configured on the server.")},
    summary="Fetch and import new leads from the enabled forms now",
)
def sync_now(admin: CurrentAdmin, session: DbSession, client: Meta) -> MetaSyncResult:
    """Each Meta lead is stored once and imported once; repeating the sync is safe."""
    return MetaLeadService(session, admin, client).sync()


@router.get("/events", response_model=Page[MetaEventRead], summary="Leads received from Meta")
def list_events(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    status: MetaLeadStatus | None = None,
    form_ref: uuid.UUID | None = None,
) -> Page[MetaEventRead]:
    """Newest first. Search matches Meta's lead id."""
    items, total = MetaLeadService(session, admin, None).events(query, status, form_ref)
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/events/{event_id}", response_model=MetaEventRead, responses=NOT_FOUND, summary="One Meta lead"
)
def get_event(admin: CurrentAdmin, session: DbSession, event_id: uuid.UUID) -> MetaEventRead:
    return MetaLeadService(session, admin, None).event(event_id)


@router.post(
    "/events/{event_id}/reprocess",
    response_model=MetaEventRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Try a failed Meta lead again",
)
def reprocess_event(admin: CurrentAdmin, session: DbSession, event_id: uuid.UUID) -> MetaEventRead:
    """Uses the form's current mapping. An imported lead is never imported again."""
    return MetaLeadService(session, admin, None).reprocess(event_id)
