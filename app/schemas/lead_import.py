"""Lead spreadsheet import (Phase 3B-1): preview, results and history."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.enums import LeadImportFileType
from app.schemas.common import ReadSchema
from app.schemas.crm import DuplicateMatch


class ImportField(ReadSchema):
    key: str
    label: str = Field(description="The template's column header for this field.")


class ImportColumn(ReadSchema):
    index: int
    header: str
    field: str | None = Field(description="The lead field it fills; null when ignored.")
    recognised: bool = Field(description="Whether the header itself names a template column.")


class CellProblem(ReadSchema):
    column: str
    field: str | None
    message: str


class PreviewRow(ReadSchema):
    row: int = Field(description="The row number as the spreadsheet shows it.")
    status: Literal["READY", "ERROR", "DUPLICATE"]
    values: dict[str, str] = Field(description="Field -> the value as given in the file.")
    errors: list[CellProblem]
    duplicates: list[DuplicateMatch] = Field(description="Existing leads and tenants it may be.")
    repeated_in_file: list[int] = Field(
        description="Other rows of this file with the same email, phone or company."
    )


class PreviousImport(ReadSchema):
    id: uuid.UUID
    file_name: str
    created_at: datetime
    imported_count: int
    created_by_email: str | None


class ImportPreview(ReadSchema):
    """Nothing is saved by a preview."""

    file_name: str
    file_type: LeadImportFileType
    fingerprint: str = Field(description="SHA-256 of the file; send it back to import.")
    columns: list[ImportColumn]
    fields: list[ImportField]
    unknown_columns: list[str] = Field(description="Headers that name no template column.")
    total_rows: int
    ready_count: int
    error_count: int
    duplicate_count: int
    rows: list[PreviewRow]
    previous_imports: list[PreviousImport] = Field(
        description="Earlier imports of a file with the same fingerprint. A warning only."
    )


class DuplicateBrief(ReadSchema):
    kind: Literal["LEAD", "TENANT"]
    id: uuid.UUID
    reference: str | None
    name: str
    matched_on: list[str]


class ImportResultRow(ReadSchema):
    row: int
    outcome: Literal["IMPORTED", "SKIPPED", "ERROR"]
    lead_id: uuid.UUID | None = None
    lead_reference: str | None = None
    label: str | None = Field(default=None, description="Company and contact, as imported.")
    values: dict[str, str] = Field(
        default_factory=dict, description="As given in the file; kept for skipped and error rows."
    )
    errors: list[CellProblem] = Field(default_factory=list)
    duplicates: list[DuplicateBrief] = Field(default_factory=list)
    repeated_in_file: list[int] = Field(default_factory=list)


class ImportBatchSummary(ReadSchema):
    id: uuid.UUID
    file_name: str
    file_type: LeadImportFileType
    file_sha256: str
    total_rows: int
    imported_count: int
    skipped_count: int
    error_count: int
    duplicate_count: int = Field(description="Imported or skipped rows that had a warning.")
    created_by_email: str | None
    created_by_name: str | None
    created_at: datetime


class ImportBatchRead(ImportBatchSummary):
    columns: list[ImportColumn]
    rows: list[ImportResultRow]
