"""ORM models. Import every model module here so Alembic autogenerate sees it."""

from app.models.account import Account
from app.models.activity import ActivityLog
from app.models.agreement import ContractDocument, ContractSignedCopy, ContractTemplate
from app.models.allocation import Allocation, AllocationClaim
from app.models.amenity import Amenity, PropertyAmenity
from app.models.asset import Asset
from app.models.booking import Booking
from app.models.building import Building, Floor
from app.models.company import CompanyProfile
from app.models.crm import (
    Lead,
    LeadImportBatch,
    LeadInterest,
    LeadNote,
    LeadTask,
    Proposal,
    ProposalSpace,
)
from app.models.file import StoredFile
from app.models.layout import Layout
from app.models.meta import MetaLeadEvent, MetaLeadForm
from app.models.pricing import BillingProfile, PricingRule
from app.models.property import Property
from app.models.reimbursement import (
    ReimbursementExpense,
    ReimbursementExpenseType,
    ReimbursementTerm,
)
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser

__all__ = [
    "ActivityLog",
    "Allocation",
    "AllocationClaim",
    "Amenity",
    "AppUser",
    "Asset",
    "BillingProfile",
    "CompanyProfile",
    "ContractDocument",
    "ContractSignedCopy",
    "ContractTemplate",
    "Booking",
    "Building",
    "Floor",
    "Layout",
    "Account",
    "Lead",
    "LeadImportBatch",
    "LeadInterest",
    "LeadNote",
    "LeadTask",
    "MetaLeadEvent",
    "MetaLeadForm",
    "Proposal",
    "ProposalSpace",
    "Property",
    "PricingRule",
    "PropertyAmenity",
    "ReimbursementExpense",
    "ReimbursementExpenseType",
    "ReimbursementTerm",
    "Space",
    "StoredFile",
    "Tenant",
    "TenantOccupancy",
]
