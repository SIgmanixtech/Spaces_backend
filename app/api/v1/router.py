"""Version 1 API router.

Every endpoint lives under one of two prefixes, so a path (and every log line) shows
which application it serves:

    /api/v1/admin/...   the SIGMANIX admin portal (administrators only)
    /api/v1/client/...  the customer app: sign-in, public catalogue and the customer's own data
    /api/v1/health      shared, for the hosting platform's health check

The older unprefixed paths (/api/v1/properties, /api/v1/customer/me, ...) still answer, so
frontends deployed before this change keep working until they are redeployed. They are
hidden from the API docs and will be removed later.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    accounts,
    activity,
    agreements,
    amenities,
    assets,
    auth,
    bookings,
    buildings,
    company_profiles,
    contracts,
    crm,
    customer,
    files,
    floors,
    health,
    layouts,
    meta_leads,
    pricing,
    properties,
    property_dashboard,
    public,
    reimbursements,
    spaces,
    tenants,
)

ADMIN_ROUTERS = (
    auth.router,
    property_dashboard.router,
    activity.router,
    properties.router,
    buildings.router,
    floors.router,
    layouts.router,
    spaces.router,
    bookings.router,
    amenities.router,
    tenants.router,
    tenants.occupancy_router,
    assets.router,
    files.router,
    contracts.router,
    agreements.router,
    company_profiles.router,
    pricing.router,
    pricing.property_router,
    pricing.profile_router,
    reimbursements.router,
    crm.router,
    meta_leads.router,
    accounts.router,
)

admin_api = APIRouter(prefix="/admin")
for router in ADMIN_ROUTERS:
    admin_api.include_router(router, tags=["Admin"])

client_api = APIRouter(prefix="/client")
client_api.include_router(auth.router, tags=["Client"])  # password sign-in, refresh, sign-out
client_api.include_router(public.router, tags=["Client"])  # /client/public/...
client_api.include_router(customer.router, tags=["Client"])  # /client/me, /client/bookings, ...

# The paths used before the split. Same handlers; hidden from the docs.
legacy_api = APIRouter(include_in_schema=False)
for router in ADMIN_ROUTERS:
    legacy_api.include_router(router)
legacy_api.include_router(public.router)
legacy_api.include_router(customer.router, prefix="/customer")

# The URL shape chosen on 3 Nov 2026, mounted by app.main: /admin/api/v1/... for the admin
# app and /client/api/v1/... for the client app. These are the documented paths.
admin_root = APIRouter()
for router in ADMIN_ROUTERS:
    admin_root.include_router(router, tags=["Admin"])

client_root = APIRouter()
client_root.include_router(auth.router, tags=["Client"])
client_root.include_router(public.router, tags=["Client"])
client_root.include_router(customer.router, tags=["Client"])

# Under /api/v1: the health checks (documented), and the earlier /api/v1/admin/...,
# /api/v1/client/... and unprefixed paths. Same handlers, still answering so existing apps
# keep working, but hidden from the docs.
api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(admin_api, include_in_schema=False)
api_router.include_router(client_api, include_in_schema=False)
api_router.include_router(legacy_api)
