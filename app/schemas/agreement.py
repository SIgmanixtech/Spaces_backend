import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.enums import ContractType, DocumentStatus, TemplateStatus
from app.schemas.common import InputSchema, ReadSchema


class TemplateRead(ReadSchema):
    id: uuid.UUID
    contract_type: ContractType
    property_id: uuid.UUID | None = Field(description="Null: the default for every property.")
    name: str
    version: int
    status: TemplateStatus
    original_name: str
    size_bytes: int
    placeholders: list[str]
    unknown_placeholders: list[str] = Field(
        default_factory=list, description="Placeholders the system cannot fill; block approval."
    )
    required_fields: list[str]
    notes: str | None
    uploaded_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    retired_at: datetime | None
    created_at: datetime


class TemplateSummary(ReadSchema):
    id: uuid.UUID
    name: str
    version: int
    contract_type: ContractType


class AgreementField(ReadSchema):
    key: str
    label: str
    hint: str = ""
    choices: list[str] = Field(default_factory=list)
    value: str | None = Field(description="What the customer saved.")
    suggested: str | None = Field(description="From the customer's record, until they save.")


class DocumentRead(ReadSchema):
    id: uuid.UUID
    status: DocumentStatus
    file_name: str
    contract_type: ContractType
    template_id: uuid.UUID
    template_version: int
    size_bytes: int
    created_at: datetime
    approved_at: datetime | None
    sent_at: datetime | None
    signed_at: datetime | None
    superseded_at: datetime | None


class AgreementRead(ReadSchema):
    """Where a contract's agreement stands, and what is still needed to generate it."""

    contract_id: uuid.UUID
    reference: str
    contract_status: str
    contract_type: ContractType | None
    contract_type_label: str | None
    type_source: Literal["contract", "space"] | None
    template: TemplateSummary | None
    fields: list[AgreementField]
    missing: list[str] = Field(description="Labels of values still needed.")
    problem: str | None = Field(description="Why the agreement cannot be generated yet.")
    can_generate: bool
    documents: list[DocumentRead]


class AgreementDetails(InputSchema):
    values: dict[str, str | None] = Field(
        description="Customer fields by key, e.g. legal_name, address, gstin."
    )


class LinkRead(ReadSchema):
    url: str
    expires_in: int


class FieldCatalogue(ReadSchema):
    customer_fields: list[AgreementField]
    placeholders: dict[ContractType, dict[str, str]] = Field(
        description="Every placeholder a template of each type may use, with its meaning."
    )
