"""Version 1 API router."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    amenities,
    assets,
    auth,
    bookings,
    buildings,
    contracts,
    customer,
    files,
    floors,
    health,
    layouts,
    pricing,
    properties,
    property_dashboard,
    public,
    spaces,
    tenants,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(property_dashboard.router)
api_router.include_router(properties.router)
api_router.include_router(buildings.router)
api_router.include_router(floors.router)
api_router.include_router(layouts.router)
api_router.include_router(spaces.router)
api_router.include_router(bookings.router)
api_router.include_router(amenities.router)
api_router.include_router(tenants.router)
api_router.include_router(tenants.occupancy_router)
api_router.include_router(assets.router)
api_router.include_router(files.router)
api_router.include_router(contracts.router)
api_router.include_router(pricing.router)
api_router.include_router(pricing.property_router)
api_router.include_router(pricing.profile_router)
api_router.include_router(public.router)
api_router.include_router(customer.router)
