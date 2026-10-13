import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import OccupancyStatus, TenantStatus
from app.schemas.common import Page
from app.schemas.tenant import (
    OccupancyCreate,
    OccupancyRead,
    OccupancyUpdate,
    TenantCreate,
    TenantRead,
    TenantUpdate,
)
from app.services.tenants import TenantService

router = APIRouter(prefix="/tenants", tags=["Tenants"], responses=PROTECTED)
occupancy_router = APIRouter(
    prefix="/occupancies", tags=["Tenants"], responses={**PROTECTED, **NOT_FOUND}
)


@router.get("", response_model=Page[TenantRead], summary="List tenants")
def list_tenants(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    status: TenantStatus | None = None,
    occupancy_status: Annotated[
        OccupancyStatus | None, Query(description="Derived occupancy status.")
    ] = None,
    property_id: Annotated[
        uuid.UUID | None,
        Query(description="Tenants with a current or upcoming occupancy at this property."),
    ] = None,
    include_archived: bool = False,
) -> Page[TenantRead]:
    """Search matches name, company, email and phone. Sort by `name`,
    `company_name`, `status`, `created_at` or `updated_at`."""
    items, total = TenantService(session, admin).search(
        query,
        {"status": status},
        property_id=property_id,
        occupancy_status=occupancy_status,
        include_archived=include_archived,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post("", response_model=TenantRead, status_code=201, summary="Create a tenant")
def create_tenant(admin: CurrentAdmin, session: DbSession, body: TenantCreate) -> TenantRead:
    service = TenantService(session, admin)
    return service.to_read([service.create(body.model_dump())])[0]


@router.get("/{tenant_id}", response_model=TenantRead, responses=NOT_FOUND, summary="Get a tenant")
def get_tenant(admin: CurrentAdmin, session: DbSession, tenant_id: uuid.UUID) -> TenantRead:
    service = TenantService(session, admin)
    return service.to_read([service.get(tenant_id)])[0]


@router.patch(
    "/{tenant_id}", response_model=TenantRead, responses=NOT_FOUND, summary="Update a tenant"
)
def update_tenant(
    admin: CurrentAdmin, session: DbSession, tenant_id: uuid.UUID, body: TenantUpdate
) -> TenantRead:
    service = TenantService(session, admin)
    return service.to_read([service.update(tenant_id, body.model_dump(exclude_unset=True))])[0]


@router.delete(
    "/{tenant_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Archive a tenant",
)
def archive_tenant(admin: CurrentAdmin, session: DbSession, tenant_id: uuid.UUID) -> None:
    """Archive rather than delete. Refused while the tenant has a current occupancy."""
    TenantService(session, admin).delete(tenant_id)


@router.get(
    "/{tenant_id}/occupancies",
    response_model=list[OccupancyRead],
    responses=NOT_FOUND,
    summary="List a tenant's occupancy history",
)
def list_occupancies(
    admin: CurrentAdmin, session: DbSession, tenant_id: uuid.UUID
) -> list[OccupancyRead]:
    """Past, current and future occupancies, newest first."""
    return TenantService(session, admin).list_occupancies(tenant_id)


@router.post(
    "/{tenant_id}/occupancies",
    response_model=OccupancyRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Assign a tenant to a property or space",
)
def create_occupancy(
    admin: CurrentAdmin, session: DbSession, tenant_id: uuid.UUID, body: OccupancyCreate
) -> OccupancyRead:
    """Dates are calendar days in the business time zone; `end_date` is inclusive.
    Returns 409 when another tenant already occupies the space on any of those days."""
    return TenantService(session, admin).create_occupancy(tenant_id, body.model_dump())


@occupancy_router.patch(
    "/{occupancy_id}",
    response_model=OccupancyRead,
    responses=CONFLICT,
    summary="Update or end an occupancy",
)
def update_occupancy(
    admin: CurrentAdmin, session: DbSession, occupancy_id: uuid.UUID, body: OccupancyUpdate
) -> OccupancyRead:
    """Set `end_date` to end an occupancy while keeping it in the history."""
    return TenantService(session, admin).update_occupancy(
        occupancy_id, body.model_dump(exclude_unset=True)
    )


@occupancy_router.delete("/{occupancy_id}", status_code=204, summary="Delete an occupancy")
def delete_occupancy(admin: CurrentAdmin, session: DbSession, occupancy_id: uuid.UUID) -> None:
    """Removes the record entirely. Use this only to correct a mistake."""
    TenantService(session, admin).delete_occupancy(occupancy_id)
