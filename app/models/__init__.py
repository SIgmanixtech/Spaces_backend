"""ORM models. Import every model module here so Alembic autogenerate sees it."""

from app.models.activity import ActivityLog
from app.models.amenity import Amenity, PropertyAmenity
from app.models.asset import Asset
from app.models.booking import Booking
from app.models.building import Building, Floor
from app.models.file import StoredFile
from app.models.layout import Layout
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser

__all__ = [
    "ActivityLog",
    "Amenity",
    "AppUser",
    "Asset",
    "Booking",
    "Building",
    "Floor",
    "Layout",
    "Property",
    "PropertyAmenity",
    "Space",
    "StoredFile",
    "Tenant",
    "TenantOccupancy",
]
