"""Company profiles (Phase 4B): the legal entity that owns properties and signs their
agreements. Administrators only; never exposed to the client app."""

import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.schemas.company import (
    CompanyProfileCreate,
    CompanyProfileRead,
    CompanyProfileUpdate,
    ProfileProperties,
)
from app.services.company_profiles import CompanyProfileService

router = APIRouter(prefix="/company-profiles", tags=["Company profiles"], responses=PROTECTED)
BAD = {400: _error("A property id does not exist.")}


@router.get("", response_model=list[CompanyProfileRead], summary="List company profiles")
def list_profiles(admin: CurrentAdmin, session: DbSession) -> list[CompanyProfileRead]:
    return CompanyProfileService(session, admin).list()


@router.post(
    "", response_model=CompanyProfileRead, status_code=201, summary="Create a company profile"
)
def create_profile(
    admin: CurrentAdmin, session: DbSession, body: CompanyProfileCreate
) -> CompanyProfileRead:
    return CompanyProfileService(session, admin).create(body.model_dump())


@router.get(
    "/{profile_id}",
    response_model=CompanyProfileRead,
    responses=NOT_FOUND,
    summary="A company profile",
)
def get_profile(
    admin: CurrentAdmin, session: DbSession, profile_id: uuid.UUID
) -> CompanyProfileRead:
    return CompanyProfileService(session, admin).get(profile_id)


@router.patch(
    "/{profile_id}",
    response_model=CompanyProfileRead,
    responses=NOT_FOUND,
    summary="Update a company profile",
)
def update_profile(
    admin: CurrentAdmin, session: DbSession, profile_id: uuid.UUID, body: CompanyProfileUpdate
) -> CompanyProfileRead:
    """Agreements already generated keep the values they were made with."""
    return CompanyProfileService(session, admin).update(
        profile_id, body.model_dump(exclude_unset=True)
    )


@router.put(
    "/{profile_id}/properties",
    response_model=CompanyProfileRead,
    responses={**NOT_FOUND, **BAD},
    summary="Set the properties this profile owns",
)
def set_properties(
    admin: CurrentAdmin, session: DbSession, profile_id: uuid.UUID, body: ProfileProperties
) -> CompanyProfileRead:
    return CompanyProfileService(session, admin).set_properties(profile_id, body.property_ids)
