"""The signed-in customer's own account, bookings and contracts (role CUSTOMER).

No request names a tenant or customer: the account comes from the access token,
and any id that is not the customer's own answers 404.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import CurrentCustomer, DbSession, ListParams, get_token_verifier
from app.api.responses import NOT_FOUND, PROTECTED, VALIDATION_ERROR, _error
from app.api.v1.endpoints.files import StorageFactory
from app.core.security import TokenVerifier
from app.models.enums import BookingStatus
from app.schemas.client import (
    CustomerBooking,
    CustomerBookingCancel,
    CustomerBookingCreate,
    CustomerBookingReschedule,
    CustomerContract,
    CustomerDashboard,
    CustomerProfile,
    CustomerProfileUpdate,
    CustomerRegister,
    PasswordRecovery,
    RegistrationResult,
)
from app.schemas.common import Page
from app.services.auth import SupabaseAuthGateway, get_auth_gateway
from app.services.catalogue import CatalogueService
from app.services.customers import CustomerService, register

router = APIRouter(prefix="/customer", tags=["Customer"])
Gateway = Annotated[SupabaseAuthGateway, Depends(get_auth_gateway)]
Verifier = Annotated[TokenVerifier, Depends(get_token_verifier)]
HELD = {409: _error("The space is not free for that time, or has no price for that period.")}


@router.post(
    "/register",
    response_model=RegistrationResult,
    status_code=201,
    responses={
        **VALIDATION_ERROR,
        409: _error("An account with this email already exists."),
        503: _error("The authentication service is unavailable."),
    },
    summary="Create a customer account",
)
def register_customer(
    body: CustomerRegister, session: DbSession, gateway: Gateway, verifier: Verifier
) -> RegistrationResult:
    """Public. Creates the sign-in (Supabase Auth), a CUSTOMER account and the tenant
    record it owns. Signed in at once, or after confirming the email, depending on the
    authentication provider's settings. Sign in later with /auth/login."""
    return register(
        session,
        gateway,
        verifier,
        email=str(body.email),
        password=body.password.get_secret_value(),
        full_name=body.full_name,
        phone=body.phone,
        company_name=body.company_name,
    )


@router.post(
    "/password-recovery", status_code=202, responses=VALIDATION_ERROR, summary="Reset password"
)
def password_recovery(body: PasswordRecovery, gateway: Gateway) -> dict[str, str]:
    """Public. Sends a reset link if the address has an account; the answer is the same
    either way."""
    gateway.recover(str(body.email))
    return {"message": "If an account uses that email, a reset link is on its way."}


@router.post(
    "/resend-confirmation",
    status_code=202,
    responses=VALIDATION_ERROR,
    summary="Resend the confirmation email",
)
def resend_confirmation(body: PasswordRecovery, gateway: Gateway) -> dict[str, str]:
    """Public. Sends the sign-up confirmation link again if the address has an unconfirmed
    account; the answer is the same either way."""
    gateway.resend_confirmation(str(body.email))
    return {"message": "If that address is waiting for confirmation, a new link is on its way."}


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
