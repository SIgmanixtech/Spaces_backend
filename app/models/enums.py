"""Allowed values for status and type columns.

Stored as text with CHECK constraints (see ``app.models.mixins.enum_check``)
rather than PostgreSQL enum types, so adding a value is a simple migration.
"""

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    # A customer of the client app: sees published properties and only their own records.
    CUSTOMER = "CUSTOMER"


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
    # A workstation assigned to one occupant for the whole term.
    DEDICATED_WORKSTATION = "DEDICATED_WORKSTATION"
    # A hot desk: any member may use it, booked by the hour or day.
    FLEXIBLE_WORKSTATION = "FLEXIBLE_WORKSTATION"
    # One seat. Booking it holds that seat only, never its neighbours.
    SEAT = "SEAT"
    # Any other rentable space (storage, kiosk, studio...).
    OTHER = "OTHER"


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
    ASSET = "ASSET"


class ContractStatus(StrEnum):
    """Stored lifecycle of a contract (a tenant occupancy).

    PENDING     drafted; holds no inventory
    ACTIVE      holds its space (and everything inside it) for its dates
    CANCELLED   withdrawn before taking effect, or voided; holds nothing
    TERMINATED  ended early; holds its dates up to the termination date

    EXPIRED is not stored: an ACTIVE contract whose last day has passed reads as EXPIRED.
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    TERMINATED = "TERMINATED"


# Contract statuses whose dates hold inventory.
HOLDING_CONTRACT_STATUSES = (ContractStatus.ACTIVE, ContractStatus.TERMINATED)


class AllocationSource(StrEnum):
    BOOKING = "BOOKING"
    CONTRACT = "CONTRACT"


class PricingTarget(StrEnum):
    """What a pricing rule prices: the property as a default, or one space."""

    PROPERTY = "PROPERTY"
    BUILDING = "BUILDING"
    SPACE = "SPACE"


class RateBasis(StrEnum):
    """What the amount is multiplied by. Separate from how often it is charged."""

    PER_SQFT = "PER_SQFT"
    PER_PERSON = "PER_PERSON"
    PER_FLOOR = "PER_FLOOR"
    PER_PORTION = "PER_PORTION"
    PER_CABIN = "PER_CABIN"
    PER_OFFICE = "PER_OFFICE"
    PER_WORKSTATION = "PER_WORKSTATION"
    PER_SEAT = "PER_SEAT"
    PER_ROOM = "PER_ROOM"
    PER_HALL = "PER_HALL"
    PER_UNIT = "PER_UNIT"


class BillingPeriod(StrEnum):
    """How long one charge of the amount covers."""

    HOURLY = "HOURLY"
    HALF_DAY = "HALF_DAY"
    FULL_DAY = "FULL_DAY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    CUSTOM = "CUSTOM"


class PricingStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class MaintenanceBasis(StrEnum):
    PER_SQFT = "PER_SQFT"
    FIXED = "FIXED"


class TaxTreatment(StrEnum):
    EXEMPT = "EXEMPT"
    INCLUDED = "INCLUDED"
    ADDITIONAL = "ADDITIONAL"


class DepositRule(StrEnum):
    NONE = "NONE"
    FIXED = "FIXED"
    MONTHS_OF_RENT = "MONTHS_OF_RENT"


class PaymentFrequency(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    HALF_YEARLY = "HALF_YEARLY"
    ANNUALLY = "ANNUALLY"
    PER_BOOKING = "PER_BOOKING"
    CUSTOM = "CUSTOM"


class BookingChannel(StrEnum):
    """Where a booking was made: by an administrator, or by a customer in the client app."""

    ADMIN = "ADMIN"
    CUSTOMER = "CUSTOMER"


class RateSource(StrEnum):
    """Where a price came from.

    CONFIGURED  entered by an administrator
    DERIVED     calculated from another rule with a billing profile, then approved
    OVERRIDE    entered by an administrator to replace a derived rate for its period
    """

    CONFIGURED = "CONFIGURED"
    DERIVED = "DERIVED"
    OVERRIDE = "OVERRIDE"


class RoundingPolicy(StrEnum):
    """How derived amounts are rounded (half up)."""

    PAISA = "PAISA"
    RUPEE = "RUPEE"
    TEN_RUPEES = "TEN_RUPEES"
