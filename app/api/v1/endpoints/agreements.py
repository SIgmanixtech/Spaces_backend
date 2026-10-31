"""Contract agreement templates and generated documents (administrators)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, UploadFile

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.api.v1.endpoints.files import Storage
from app.core.errors import BadRequestError
from app.models.agreement import ContractTemplate
from app.models.enums import ContractType, DocumentStatus, TemplateStatus
from app.models.tenant import TenantOccupancy
from app.schemas.agreement import (
    AgreementField,
    AgreementRead,
    DocumentRead,
    FieldCatalogue,
    LinkRead,
    TemplateRead,
    TemplateSummary,
)
from app.services.agreements import (
    CUSTOMER_FIELDS,
    MAX_TEMPLATE_BYTES,
    AgreementService,
    allowed_placeholders,
    unknown_placeholders,
)
from app.services.base import require

router = APIRouter(tags=["Contract agreements"], responses=PROTECTED)
CONFLICT = {409: _error("Not allowed in the current state; the message says why.")}


def template_read(template: ContractTemplate) -> TemplateRead:
    read = TemplateRead.model_validate(template)
    read.unknown_placeholders = unknown_placeholders(
        template.placeholders, ContractType(template.contract_type)
    )
    return read


def agreement_read(service: AgreementService, contract: TenantOccupancy) -> AgreementRead:
    plan = service.plan(contract)
    template = plan["template"]
    return AgreementRead(
        contract_id=contract.id,
        reference=contract.reference,
        contract_status=contract.status,
        contract_type=plan["contract_type"],
        contract_type_label=plan["contract_type_label"],
        type_source=plan["type_source"],
        template=TemplateSummary.model_validate(template) if template else None,
        fields=[AgreementField(**f) for f in plan["fields"]],
        missing=plan["missing"],
        problem=plan["problem"],
        can_generate=plan["can_generate"],
        documents=[DocumentRead.model_validate(d) for d in service.documents(contract.id)],
    )


@router.get(
    "/contract-templates/catalogue",
    response_model=FieldCatalogue,
    summary="Customer fields and placeholders a template may use",
)
def catalogue(admin: CurrentAdmin) -> FieldCatalogue:
    return FieldCatalogue(
        customer_fields=[
            AgreementField(
                key=f.key,
                label=f.label,
                hint=f.hint,
                choices=list(f.choices),
                value=None,
                suggested=None,
            )
            for f in CUSTOMER_FIELDS.values()
        ],
        placeholders={t: allowed_placeholders(t) for t in ContractType},
    )


@router.get(
    "/contract-templates", response_model=list[TemplateRead], summary="List contract templates"
)
def list_templates(
    admin: CurrentAdmin,
    session: DbSession,
    contract_type: ContractType | None = None,
    status: TemplateStatus | None = None,
) -> list[TemplateRead]:
    return [
        template_read(t) for t in AgreementService(session, admin).templates(contract_type, status)
    ]


@router.post(
    "/contract-templates",
    response_model=TemplateRead,
    status_code=201,
    responses={400: _error("Not a usable Word document or an unknown field.")},
    summary="Upload a contract template (DOCX); it starts as a draft",
)
def upload_template(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    file: UploadFile,
    contract_type: Annotated[ContractType, Form()],
    name: Annotated[str, Form(min_length=1, max_length=200)],
    property_id: Annotated[uuid.UUID | None, Form()] = None,
    required_fields: Annotated[
        str, Form(description="Comma-separated customer field keys, e.g. legal_name,address")
    ] = "",
    notes: Annotated[str | None, Form(max_length=2000)] = None,
) -> TemplateRead:
    data = file.file.read(MAX_TEMPLATE_BYTES + 1)
    if not data:
        raise BadRequestError("The file is empty.")
    return template_read(
        AgreementService(session, admin, storage).upload_template(
            contract_type=contract_type,
            name=name,
            data=data,
            original_name=file.filename or "template.docx",
            property_id=property_id,
            required_fields=[f.strip() for f in required_fields.split(",") if f.strip()],
            notes=notes,
        )
    )


@router.post(
    "/contract-templates/{template_id}/approve",
    response_model=TemplateRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Approve a draft; it becomes the active template for its type and scope",
)
def approve_template(
    admin: CurrentAdmin, session: DbSession, template_id: uuid.UUID
) -> TemplateRead:
    return template_read(AgreementService(session, admin).approve_template(template_id))


@router.post(
    "/contract-templates/{template_id}/retire",
    response_model=TemplateRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Retire a template; documents already made from it keep it",
)
def retire_template(
    admin: CurrentAdmin, session: DbSession, template_id: uuid.UUID
) -> TemplateRead:
    return template_read(AgreementService(session, admin).retire_template(template_id))


@router.get(
    "/contract-templates/{template_id}/download",
    response_model=LinkRead,
    responses=NOT_FOUND,
    summary="A short-lived link to the template file",
)
def download_template(
    admin: CurrentAdmin, session: DbSession, storage: Storage, template_id: uuid.UUID
) -> LinkRead:
    return LinkRead(
        url=AgreementService(session, admin, storage).template_link(template_id), expires_in=600
    )


@router.get(
    "/contracts/{contract_id}/agreement",
    response_model=AgreementRead,
    responses=NOT_FOUND,
    summary="The contract's agreement: type, template, details and documents",
)
def contract_agreement(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> AgreementRead:
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    return agreement_read(AgreementService(session, admin), contract)


@router.post(
    "/contracts/{contract_id}/agreement/documents",
    response_model=DocumentRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, 400: _error("Details are missing.")},
    summary="Generate the agreement document (same inputs return the same document)",
)
def generate_document(
    admin: CurrentAdmin, session: DbSession, storage: Storage, contract_id: uuid.UUID
) -> DocumentRead:
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    return DocumentRead.model_validate(AgreementService(session, admin, storage).generate(contract))


def _advance(to: DocumentStatus, summary: str):  # type: ignore[no-untyped-def]
    def endpoint(admin: CurrentAdmin, session: DbSession, document_id: uuid.UUID) -> DocumentRead:
        return DocumentRead.model_validate(
            AgreementService(session, admin).advance(document_id, to)
        )

    endpoint.__doc__ = summary
    return endpoint


for _path, _to, _summary in (
    ("approve", DocumentStatus.APPROVED, "Approve a generated agreement"),
    ("send", DocumentStatus.SENT_FOR_SIGNATURE, "Record that it was sent for signature"),
    ("sign", DocumentStatus.SIGNED, "Record that it was signed"),
):
    router.add_api_route(
        f"/contract-documents/{{document_id}}/{_path}",
        _advance(_to, _summary),
        methods=["POST"],
        response_model=DocumentRead,
        responses={**NOT_FOUND, **CONFLICT},
        summary=_summary,
    )


@router.get(
    "/contract-documents/{document_id}/download",
    response_model=LinkRead,
    responses=NOT_FOUND,
    summary="A short-lived link to a generated agreement",
)
def download_document(
    admin: CurrentAdmin, session: DbSession, storage: Storage, document_id: uuid.UUID
) -> LinkRead:
    from app.models.agreement import ContractDocument

    document = require(session, ContractDocument, document_id, "Document")
    return LinkRead(
        url=AgreementService(session, admin, storage).document_link(document), expires_in=300
    )
