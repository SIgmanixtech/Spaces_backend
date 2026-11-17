import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED, _error
from app.api.v1.endpoints.files import Storage, StorageFactory
from app.models.enums import OccupancyStatus, TenantStatus
from app.schemas.common import Page, RemovalRead
from app.schemas.crm import DuplicateMatch
from app.schemas.tenant import (
    ClientAccessRead,
    ClientAccessRequest,
    OccupancyCreate,
    OccupancyRead,
    OccupancyUpdate,
    TenantCreate,
    TenantDuplicateCheck,
    TenantRead,
    TenantUpdate,
)
from app.schemas.tenant_onboarding import (
    AssignmentInput,
    AssignmentQuote,
    OnboardResult,
    SignatureLink,
    TenantOnboard,
)
from app.services.auth import SupabaseAuthGateway, get_auth_gateway
from app.services.client_access import ClientAccessService
from app.services.crm import CrmService
from app.services.tenant_onboarding import TenantOnboardingService
from app.services.tenant_signature import (
    SIGNATURE_MAX_BYTES,
    TenantSignatureService,
    check_signature,
)
from app.services.tenants import TenantService

Gateway = Annotated[SupabaseAuthGateway, Depends(get_auth_gateway)]

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


@router.post(
    "/assignment-quote",
    response_model=AssignmentQuote,
    responses={**NOT_FOUND},
    summary="Preview a space for a new tenant: availability, pricing and the first invoice",
)
def assignment_quote(
    admin: CurrentAdmin, session: DbSession, body: AssignmentInput
) -> AssignmentQuote:
    """Nothing is saved. Prices come from the space's monthly pricing rule (rent and
    maintenance per sq ft or fixed, tax and deposit as the rule says); the invoice preview
    is the draft that adding the tenant would create."""
    return TenantOnboardingService(session, admin).quote(body.model_dump())


@router.post(
    "/onboard",
    response_model=OnboardResult,
    status_code=201,
    responses={
        **NOT_FOUND,
        409: _error(
            "A tenant with the same email or phone exists (code duplicate_tenant, matches in "
            "conflicts), or the space is taken."
        ),
    },
    summary="Add a tenant (or link an existing one) with a space, account and first invoice",
)
def onboard_tenant(
    admin: CurrentAdmin,
    session: DbSession,
    storage: StorageFactory,
    data: Annotated[str, Form(description="TenantOnboard as JSON.")],
    signature: UploadFile | None = None,
) -> OnboardResult:
    """Multipart form: `data` (JSON) and an optional `signature` (PNG or JPG, at most
    2 MB). One transaction: the tenant, an ACTIVE contract for the space (the allocation
    engine's checks apply), the account and a DRAFT initial invoice for one month from the
    start date. Repeating the same request returns the same records."""
    try:
        body = TenantOnboard.model_validate_json(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from None
    upload = None
    if signature is not None:
        upload = check_signature(signature.file.read(SIGNATURE_MAX_BYTES + 1))
    return TenantOnboardingService(session, admin, storage).onboard(body.model_dump(), upload)


@router.get(
    "/{tenant_id}/signature",
    response_model=SignatureLink,
    responses=NOT_FOUND,
    summary="A short-lived link to the tenant's signature image",
)
def get_signature(
    admin: CurrentAdmin, session: DbSession, storage: Storage, tenant_id: uuid.UUID
) -> SignatureLink:
    return TenantSignatureService(session, admin, storage).link(tenant_id)


@router.put(
    "/{tenant_id}/signature",
    response_model=SignatureLink,
    responses=NOT_FOUND,
    summary="Add or replace the tenant's signature image",
)
def put_signature(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    tenant_id: uuid.UUID,
    signature: UploadFile,
) -> SignatureLink:
    """PNG or JPG, at most 2 MB. A signature asset only: nothing is marked as signed."""
    return TenantSignatureService(session, admin, storage).replace(
        tenant_id, signature.file.read(SIGNATURE_MAX_BYTES + 1)
    )


@router.delete(
    "/{tenant_id}/signature",
    status_code=204,
    responses=NOT_FOUND,
    summary="Remove the tenant's signature image",
)
def delete_signature(
    admin: CurrentAdmin, session: DbSession, storage: Storage, tenant_id: uuid.UUID
) -> Response:
    TenantSignatureService(session, admin, storage).remove(tenant_id)
    return Response(status_code=204)


@router.post(
    "/check-duplicates",
    response_model=list[DuplicateMatch],
    summary="Existing tenants that may be the same person or company",
)
def check_tenant_duplicates(
    admin: CurrentAdmin, session: DbSession, body: TenantDuplicateCheck
) -> list[DuplicateMatch]:
    """Tenants with the same normalised email, phone or company (never by name alone), using
    the CRM duplicate matching. Warning only: creating or saving a tenant is never refused,
    merged or changed because of a match."""
    return CrmService(session, admin).check_duplicates(
        contact_email=body.email,
        contact_phone=body.phone,
        company_name=body.company_name,
        exclude_tenant_id=body.exclude_tenant_id,
        include_leads=False,
    )


@router.post(
    "/{tenant_id}/client-access",
    response_model=ClientAccessRead,
    responses={**NOT_FOUND, **CONFLICT, 503: _error("Supabase could not be reached.")},
    summary="Invite a tenant to the client app (or invite again)",
)
def invite_to_client_app(
    admin: CurrentAdmin,
    session: DbSession,
    gateway: Gateway,
    tenant_id: uuid.UUID,
    body: ClientAccessRequest | None = None,
) -> ClientAccessRead:
    """Only for a tenant with a completed contract (approved, agreement recorded as signed)
    and an email address. Safe to repeat: the tenant keeps one sign-in and one customer
    account. With send_email false (or when Supabase cannot send), a link to share is
    returned instead; the answer never claims an email was delivered."""
    request = body or ClientAccessRequest()
    return ClientAccessRead.model_validate(
        ClientAccessService(session, admin, gateway).invite(
            tenant_id, send_email=request.send_email
        )
    )


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
    response_model=RemovalRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Delete a tenant (archived if it has history)",
)
def archive_tenant(admin: CurrentAdmin, session: DbSession, tenant_id: uuid.UUID) -> RemovalRead:
    """Deleted for good when nothing refers to it; archived (hidden, kept for history)
    when it has bookings, leases, prices, files or other records. Refused while it is in
    current use."""
    return RemovalRead.of("Tenant", TenantService(session, admin).delete(tenant_id))


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
