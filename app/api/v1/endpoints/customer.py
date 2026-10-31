"""The signed-in customer's own account, bookings and contracts (role CUSTOMER).

No request names a tenant or customer: the account comes from the access token,
and any id that is not the customer's own answers 404.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import CurrentCustomer, DbSession, ListParams, get_token_verifier
from app.api.responses import NOT_FOUND, PROTECTED, VALIDATION_ERROR, _error
from app.api.v1.endpoints.agreements import agreement_read
from app.api.v1.endpoints.files import Storage, StorageFactory
from app.core.errors import ForbiddenError, NotFoundError
from app.core.security import TokenVerifier
from app.models.agreement import ContractDocument
from app.models.enums import BookingStatus
from app.models.user import AppUser
from app.schemas.agreement import AgreementDetails, AgreementRead, DocumentRead, LinkRead
from app.schemas.auth import SessionRead
from app.schemas.client import (
    ContractFromBooking,
    CustomerBooking,
    CustomerBookingCancel,
    CustomerBookingCreate,
    CustomerBookingReschedule,
    CustomerContract,
    CustomerDashboard,
    CustomerGroupBookingCreate,
    CustomerProfile,
    CustomerProfileUpdate,
    OtpRequest,
    OtpResult,
    OtpVerify,
    PasswordRecovery,
    PhoneOtpRequest,
    PhoneOtpVerify,
    SetPassword,
)
from app.schemas.common import Page
from app.services.agreements import AgreementService
from app.services.auth import (
    SupabaseAuthGateway,
    SupabaseSession,
    get_auth_gateway,
    open_session,
)
from app.services.catalogue import CatalogueService
from app.services.client_access import NOT_ACTIVATED, ClientAccessService
from app.services.customers import (
    CustomerService,
    finish_code_sign_in,
    normalize_phone,
)

# Mounted at /client (and at the older /customer) by the v1 router.
router = APIRouter(tags=["Client: customer"])
Gateway = Annotated[SupabaseAuthGateway, Depends(get_auth_gateway)]
Verifier = Annotated[TokenVerifier, Depends(get_token_verifier)]
HELD = {409: _error("The space is not free for that time, or has no price for that period.")}


@router.post(
    "/password-recovery", status_code=202, responses=VALIDATION_ERROR, summary="Reset password"
)
def password_recovery(body: PasswordRecovery, gateway: Gateway) -> dict[str, str]:
    """Public. Sends a reset link if the address has an account; the answer is the same
    either way."""
    gateway.recover(str(body.email))
    return {"message": "If an account uses that email, a reset link is on its way."}


@router.post(
    "/auth/set-password",
    response_model=SessionRead,
    responses={
        **VALIDATION_ERROR,
        401: _error("The link has expired or was already used."),
        403: _error("Not an invited tenant."),
    },
    summary="Set a password from an invitation or reset link",
)
def set_password(
    body: SetPassword, session: DbSession, gateway: Gateway, verifier: Verifier
) -> SessionRead:
    """Public (the link's token is the proof). Only a tenant the property team invited can
    set a password here; the first time activates their client access. Returns a session."""
    claims = verifier.verify(body.access_token)
    user = session.get(AppUser, claims.subject)
    if user is None:
        raise ForbiddenError(NOT_ACTIVATED)
    ClientAccessService(session, None, gateway).activate(
        user, body.access_token, body.password.get_secret_value()
    )
    expires_in = (
        max(0, int(claims.expires_at - datetime.now(UTC).timestamp()))
        if claims.expires_at
        else 3600
    )
    return open_session(
        session, verifier, SupabaseSession(body.access_token, body.refresh_token, expires_in)
    )


@router.get("/me", response_model=CustomerProfile, responses=PROTECTED, summary="My profile")
def my_profile(user: CurrentCustomer, session: DbSession) -> CustomerProfile:
    return CustomerService(session, user).profile()


@router.patch("/me", response_model=CustomerProfile, responses=PROTECTED, summary="Update profile")
def update_profile(
    user: CurrentCustomer, session: DbSession, body: CustomerProfileUpdate
) -> CustomerProfile:
    return CustomerService(session, user).update_profile(body.model_dump(exclude_unset=True))


@router.get(
    "/bookings", response_model=Page[CustomerBooking], responses=PROTECTED, summary="My bookings"
)
def my_bookings(
    user: CurrentCustomer,
    session: DbSession,
    query: ListParams,
    status: BookingStatus | None = None,
) -> Page[CustomerBooking]:
    items, total = CustomerService(session, user).bookings(query, status)
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "/bookings",
    response_model=CustomerBooking,
    status_code=201,
    responses={**PROTECTED, **NOT_FOUND, **HELD},
    summary="Book a space",
)
def book(
    user: CurrentCustomer,
    session: DbSession,
    storage: StorageFactory,
    body: CustomerBookingCreate,
) -> CustomerBooking:
    """Re-prices and re-checks availability on the server. The booking is PENDING until
    the property team confirms it. The time range must be whole billing periods."""
    return CustomerService(session, user).book(
        CatalogueService(session, storage),
        space_id=body.space_id,
        billing_period=body.billing_period,
        start_at=body.start_at,
        end_at=body.end_at,
        notes=body.notes,
    )


@router.post(
    "/bookings/group",
    response_model=list[CustomerBooking],
    status_code=201,
    responses={**PROTECTED, **NOT_FOUND, **HELD},
    summary="Book several spaces (e.g. 5 seats) for the same time",
)
def book_group(
    user: CurrentCustomer,
    session: DbSession,
    storage: StorageFactory,
    body: CustomerGroupBookingCreate,
) -> list[CustomerBooking]:
    """All of them are booked, or none is (409 names the space that is not free). They
    share a group id; requesting a contract for one of them covers all of them."""
    return CustomerService(session, user).book_group(
        CatalogueService(session, storage),
        space_ids=body.space_ids,
        billing_period=body.billing_period,
        start_at=body.start_at,
        end_at=body.end_at,
        notes=body.notes,
    )


@router.get(
    "/bookings/{booking_id}",
    response_model=CustomerBooking,
    responses={**PROTECTED, **NOT_FOUND},
    summary="One of my bookings",
)
def my_booking(user: CurrentCustomer, session: DbSession, booking_id: uuid.UUID) -> CustomerBooking:
    return CustomerService(session, user).booking(booking_id)


@router.post(
    "/bookings/{booking_id}/reschedule",
    response_model=CustomerBooking,
    responses={**PROTECTED, **NOT_FOUND, **HELD},
    summary="Move one of my bookings",
)
def reschedule_booking(
    user: CurrentCustomer,
    session: DbSession,
    storage: StorageFactory,
    booking_id: uuid.UUID,
    body: CustomerBookingReschedule,
) -> CustomerBooking:
    """Pending or confirmed bookings that have not started. The new time is re-priced and
    re-checked on the server, and the booking waits for the property team to confirm it."""
    return CustomerService(session, user).reschedule(
        CatalogueService(session, storage),
        booking_id,
        billing_period=body.billing_period,
        start_at=body.start_at,
        end_at=body.end_at,
    )


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=CustomerBooking,
    responses={**PROTECTED, **NOT_FOUND, 409: _error("The booking cannot be cancelled now.")},
    summary="Cancel one of my bookings",
)
def cancel_booking(
    user: CurrentCustomer,
    session: DbSession,
    booking_id: uuid.UUID,
    body: CustomerBookingCancel | None = None,
) -> CustomerBooking:
    """Pending or confirmed bookings that have not started yet."""
    return CustomerService(session, user).cancel(booking_id, body.reason if body else None)


@router.post(
    "/otp/request", status_code=202, responses=VALIDATION_ERROR, summary="Email a sign-in code"
)
def request_otp(body: OtpRequest, gateway: Gateway) -> dict[str, str]:
    """Public. Emails a 6-digit sign-in code. A new address gets an account on first sign-in."""
    gateway.send_otp(str(body.email).strip().lower())
    if getattr(gateway, "test_mode", False):
        return {
            "message": "Test mode: no code is sent. Enter the test code you were given.",
            "test_mode": "true",
        }
    return {"message": "A sign-in code is on its way. It is valid for a few minutes."}


@router.post(
    "/otp/verify",
    response_model=OtpResult,
    responses={**VALIDATION_ERROR, 401: _error("The code is wrong or has expired.")},
    summary="Check an emailed sign-in code",
)
def verify_otp(
    body: OtpVerify, session: DbSession, gateway: Gateway, verifier: Verifier
) -> OtpResult:
    """Public. SIGNED_IN with a session, or REGISTRATION_REQUIRED for a new customer, who
    then completes /customer/onboarding."""
    supabase_session = gateway.verify_otp(str(body.email).strip().lower(), body.code)
    return finish_code_sign_in(session, verifier, supabase_session)


@router.post(
    "/phone-otp/request",
    status_code=202,
    responses={**VALIDATION_ERROR, 503: _error("SMS sign-in is not configured.")},
    summary="Text a sign-in code to a mobile number",
)
def request_phone_otp(body: PhoneOtpRequest, gateway: Gateway) -> dict[str, str]:
    """Public. The answer is the same whether or not the number has an account."""
    gateway.send_phone_otp(normalize_phone(body.phone))
    if getattr(gateway, "test_mode", False):
        return {
            "message": "Test mode: no code is sent. Enter the test code you were given.",
            "test_mode": "true",
        }
    return {"message": "A sign-in code is on its way by SMS. It is valid for a few minutes."}


@router.post(
    "/phone-otp/verify",
    response_model=OtpResult,
    responses={**VALIDATION_ERROR, 401: _error("The code is wrong or has expired.")},
    summary="Check a texted sign-in code",
)
def verify_phone_otp(
    body: PhoneOtpVerify, session: DbSession, gateway: Gateway, verifier: Verifier
) -> OtpResult:
    supabase_session = gateway.verify_phone_otp(normalize_phone(body.phone), body.code)
    return finish_code_sign_in(session, verifier, supabase_session)


@router.post(
    "/bookings/{booking_id}/contract",
    response_model=CustomerContract,
    status_code=201,
    responses={**PROTECTED, **NOT_FOUND, 409: _error("The booking or space cannot be contracted.")},
    summary="Turn a reservation into a contract request",
)
def contract_from_booking(
    user: CurrentCustomer, session: DbSession, booking_id: uuid.UUID, body: ContractFromBooking
) -> CustomerContract:
    """The booking is replaced by a contract request that holds the space for its dates, so
    no other customer can take it. If the property team rejects it, the space is free again."""
    return CustomerService(session, user).request_contract(
        booking_id, body.start_date, body.months, body.notes
    )


@router.post(
    "/contracts/{contract_id}/withdraw",
    response_model=CustomerContract,
    responses={**PROTECTED, **NOT_FOUND, 409: _error("The contract is no longer a request.")},
    summary="Withdraw my contract request",
)
def withdraw_contract(
    user: CurrentCustomer, session: DbSession, contract_id: uuid.UUID
) -> CustomerContract:
    return CustomerService(session, user).withdraw_contract(contract_id)


@router.get(
    "/contracts/{contract_id}/agreement",
    response_model=AgreementRead,
    responses={**PROTECTED, **NOT_FOUND},
    summary="My contract's agreement: what is needed and its documents",
)
def my_agreement(
    user: CurrentCustomer, session: DbSession, contract_id: uuid.UUID
) -> AgreementRead:
    contract = CustomerService(session, user).own_contract(contract_id)
    return agreement_read(AgreementService(session, user), contract)


@router.put(
    "/contracts/{contract_id}/agreement/details",
    response_model=AgreementRead,
    responses={**PROTECTED, **NOT_FOUND, **VALIDATION_ERROR},
    summary="Save the details my agreement needs",
)
def save_agreement_details(
    user: CurrentCustomer, session: DbSession, contract_id: uuid.UUID, body: AgreementDetails
) -> AgreementRead:
    """Only customer fields are accepted; prices, dates, type and template are not."""
    contract = CustomerService(session, user).own_contract(contract_id)
    service = AgreementService(session, user)
    service.save_details(contract, body.values)
    return agreement_read(service, contract)


@router.post(
    "/contracts/{contract_id}/agreement/documents",
    response_model=DocumentRead,
    status_code=201,
    responses={**PROTECTED, **NOT_FOUND, 409: _error("Not ready to generate; says why.")},
    summary="Generate my agreement document",
)
def generate_my_agreement(
    user: CurrentCustomer, session: DbSession, storage: Storage, contract_id: uuid.UUID
) -> DocumentRead:
    contract = CustomerService(session, user).own_contract(contract_id)
    return DocumentRead.model_validate(AgreementService(session, user, storage).generate(contract))


@router.get(
    "/contract-documents/{document_id}/download",
    response_model=LinkRead,
    responses={**PROTECTED, **NOT_FOUND},
    summary="A short-lived link to one of my agreement documents",
)
def download_my_agreement(
    user: CurrentCustomer, session: DbSession, storage: Storage, document_id: uuid.UUID
) -> LinkRead:
    document = session.get(ContractDocument, document_id)
    if document is None:
        raise NotFoundError("Document not found.")
    # Raises 404 unless the document's contract is this customer's.
    CustomerService(session, user).own_contract(document.contract_id)
    return LinkRead(
        url=AgreementService(session, user, storage).document_link(document), expires_in=300
    )


@router.get(
    "/dashboard", response_model=CustomerDashboard, responses=PROTECTED, summary="My dashboard"
)
def my_dashboard(user: CurrentCustomer, session: DbSession) -> CustomerDashboard:
    """Upcoming bookings with their status, active leases with the next rent date, and what
    recently happened to the customer's bookings and leases."""
    return CustomerService(session, user).dashboard()


@router.get(
    "/contracts", response_model=list[CustomerContract], responses=PROTECTED, summary="My contracts"
)
def my_contracts(user: CurrentCustomer, session: DbSession) -> list[CustomerContract]:
    """Read-only: contracts are drafted, activated and ended by the property team."""
    return CustomerService(session, user).contracts()


@router.get(
    "/contracts/{contract_id}",
    response_model=CustomerContract,
    responses={**PROTECTED, **NOT_FOUND},
    summary="One of my contracts",
)
def my_contract(
    user: CurrentCustomer, session: DbSession, contract_id: uuid.UUID
) -> CustomerContract:
    return CustomerService(session, user).contract(contract_id)
