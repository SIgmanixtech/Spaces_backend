"""Allowed values for status and type columns.

Stored as text with CHECK constraints (see ``app.models.mixins.enum_check``)
rather than PostgreSQL enum types, so adding a value is a simple migration.
"""

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "ADMIN"


class OccupancyStatus(StrEnum):
    """Derived for properties and tenants from occupancies and bookings. Not stored."""

    VACANT = "VACANT"
    RESERVED = "RESERVED"
    OCCUPIED = "OCCUPIED"
    UNAVAILABLE = "UNAVAILABLE"


class SpaceStatus(StrEnum):
    """Derived for a space at the current moment. Not stored."""

    AVAILABLE = "AVAILABLE"
    BOOKED = "BOOKED"
    RESERVED = "RESERVED"
    OCCUPIED = "OCCUPIED"
    UNAVAILABLE = "UNAVAILABLE"


class PropertyStatus(StrEnum):
    """Operational state of a property. Occupancy is derived, never stored."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE"


class LayoutStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class SpaceType(StrEnum):
    DESK = "DESK"
    PRIVATE_OFFICE = "PRIVATE_OFFICE"
    CABIN = "CABIN"
    MEETING_ROOM = "MEETING_ROOM"
    COWORKING = "COWORKING"
    # The whole floor: the single root every other space on a floor descends from.
    FLOOR = "FLOOR"
    PORTION = "PORTION"
    CONFERENCE_ROOM = "CONFERENCE_ROOM"
    TRAINING_ROOM = "TRAINING_ROOM"
    # Corridors, washrooms, service areas: part of the inventory, never rented.
    NON_RENTABLE = "NON_RENTABLE"


class SpaceOperationalStatus(StrEnum):
    """Whether a space can be used at all. Time-specific availability is derived."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE"


class PriceUnit(StrEnum):
    HOUR = "HOUR"
    DAY = "DAY"
    MONTH = "MONTH"


class TenantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class BookingStatus(StrEnum):
    """Every status except CANCELLED holds the booking's time range."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class AmenityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class AssetStatus(StrEnum):
    OPERATIONAL = "OPERATIONAL"
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"
    RETIRED = "RETIRED"


class FileKind(StrEnum):
    MAIN = "MAIN"
    GALLERY = "GALLERY"
    BUILDING = "BUILDING"
    INTERIOR = "INTERIOR"
    WORKSPACE = "WORKSPACE"
    FLOOR_PLAN = "FLOOR_PLAN"
    DOCUMENT = "DOCUMENT"
