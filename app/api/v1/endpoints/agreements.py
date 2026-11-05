"""Contract agreement templates and generated documents (administrators).

Phase 4B: admins prepare and generate an agreement while the contract is still PENDING
(generating never approves, activates or holds anything), prefill details from the
tenant's record on request, fill the template's own fields, and record the signature by
uploading the paper-signed copy (PDF). Customers' endpoints are unchanged.
"""

import json
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, UploadFile
from pydantic import TypeAdapter, ValidationError

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.api.v1.endpoints.files import Storage
from app.core.errors import BadRequestError
from app.models.agreement import ContractDocument, ContractTemplate
from app.models.company import CompanyProfile
from app.models.crm import Proposal
from app.models.enums import ContractType, DocumentStatus, TemplateStatus
from app.models.property import Property
from app.models.tenant import TenantOccupancy
from app.schemas.agreement import (
    AdminAgreementRead,
    AdminDocumentRead,
    AgreementDetails,
    AgreementField,
    AgreementRead,
    CompanySummary,
    ContractFieldsInput,
    CustomFieldsInput,
    CustomFieldValue,
    DocumentRead,
    FieldCatalogue,
    LinkRead,
    PrefillApply,
    PrefillRow,
    SignedCopyRead,
    Signer,
    TemplateRead,
    TemplateSummary,
)
from app.services.agreements import (
    CUSTOMER_FIELDS,
    MAX_SIGNED_COPY_BYTES,
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
        template.placeholders, ContractType(template.contract_type), template.custom_fields
    )
    read.unused_custom_fields = [
        f["key"]
        for f in template.custom_fields or []
        if f"custom.{f['key']}" not in template.placeholders
    ]
    return read


LIFECYCLE = {
    DocumentStatus.GENERATED: "Generated - pending review",
    DocumentStatus.APPROVED: "Ready for signing",
    DocumentStatus.SENT_FOR_SIGNATURE: "Given for signing",
    DocumentStatus.SIGNED: "Signed - copy uploaded",
}


def admin_agreement_read(
    service: AgreementService, contract: TenantOccupancy
) -> AdminAgreementRead:
    """The admin's view: everything customers see, plus the snapshot of values, signed
    copies, the template's own fields, the source proposal and the company profile."""
    base = agreement_read(service, contract, admin=True)
    plan = service.plan(contract, admin=True)
    template: ContractTemplate | None = plan["template"]
    documents = service.documents(contract.id)
    copies = service.current_copies({d.id for d in documents})
    docs = []
    for d in documents:
        read = AdminDocumentRead.model_validate(d)
        read.values = dict(d.data or {})
        copy = copies.get(d.id)
        read.signed_copy = SignedCopyRead.model_validate(copy) if copy else None
        read.signed_without_copy = d.status == DocumentStatus.SIGNED and copy is None
        docs.append(read)
    current = next((d for d in documents if d.status != DocumentStatus.SUPERSEDED), None)
    if contract.status == "CANCELLED":
        lifecycle = "Cancelled"
    elif current is None:
        lifecycle = "Draft"
    elif current.status == DocumentStatus.SIGNED and current.id in copies:
        lifecycle = LIFECYCLE[DocumentStatus.SIGNED]
    elif current.status == DocumentStatus.SIGNED:
        lifecycle = "Signed (no copy uploaded)"
    else:
        lifecycle = LIFECYCLE[DocumentStatus(current.status)]
    entered = contract.contract_fields or {}
    session = service.session
    proposal = session.get(Proposal, contract.proposal_id) if contract.proposal_id else None
    prop = session.get(Property, contract.property_id)
    profile = (
        session.get(CompanyProfile, prop.company_profile_id)
        if prop and prop.company_profile_id
        else None
    )
    return AdminAgreementRead(
        **base.model_dump(exclude={"documents"}),
        documents=docs,
        lifecycle=lifecycle,
        custom_fields=[
            CustomFieldValue(**f, value=entered.get(f["key"]))
            for f in (template.custom_fields if template else [])
        ],
        other_charges=entered.get("other_charges"),
        proposal_id=contract.proposal_id,
        proposal_reference=proposal.reference if proposal else None,
        company_profile=CompanySummary.model_validate(profile) if profile else None,
    )


def agreement_read(
    service: AgreementService, contract: TenantOccupancy, *, admin: bool = False
) -> AgreementRead:
    """Customers call this with admin=False: their rules and response are unchanged."""
    plan = service.plan(contract, admin=admin)
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
    custom_fields: Annotated[
        str,
        Form(
            description='JSON list of the template\'s own fields: [{"key": "lock_in", '
            '"label": "Lock-in period", "type": "text", "required": true}]. Filled as '
            "{{ custom.<key> }}."
        ),
    ] = "[]",
) -> TemplateRead:
    data = file.file.read(MAX_TEMPLATE_BYTES + 1)
    if not data:
        raise BadRequestError("The file is empty.")
    fields = _json_list(custom_fields, "custom_fields")
    return template_read(
        AgreementService(session, admin, storage).upload_template(
            contract_type=contract_type,
            name=name,
            data=data,
            original_name=file.filename or "template.docx",
            property_id=property_id,
            required_fields=[f.strip() for f in required_fields.split(",") if f.strip()],
            notes=notes,
            custom_fields=fields,
        )
    )


def _json_list(text: str, name: str) -> list[dict]:
    try:
        value = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise BadRequestError(f"{name} must be a JSON list.") from exc
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise BadRequestError(f"{name} must be a JSON list of objects.")
    return value


@router.put(
    "/contract-templates/{template_id}/custom-fields",
    response_model=TemplateRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Set a draft template's own fields",
)
def set_custom_fields(
    admin: CurrentAdmin, session: DbSession, template_id: uuid.UUID, body: CustomFieldsInput
) -> TemplateRead:
    """Only a draft; an approved template's fields are fixed (upload a new version)."""
    return template_read(
        AgreementService(session, admin).set_custom_fields(
            template_id, [f.model_dump() for f in body.custom_fields]
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
    response_model=AdminAgreementRead,
    responses=NOT_FOUND,
    summary="The contract's agreement: type, template, details, values and documents",
)
def contract_agreement(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> AdminAgreementRead:
    """Can be prepared while the contract is PENDING (Phase 4B)."""
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    return admin_agreement_read(AgreementService(session, admin), contract)


@router.get(
    "/contracts/{contract_id}/agreement/prefill",
    response_model=list[PrefillRow],
    responses=NOT_FOUND,
    summary="What the tenant's record would put in the agreement details",
)
def prefill_preview(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> list[PrefillRow]:
    """Read-only: the saved value and the tenant's value side by side."""
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    return [PrefillRow(**r) for r in AgreementService(session, admin).prefill_preview(contract)]


@router.post(
    "/contracts/{contract_id}/agreement/prefill",
    response_model=AdminAgreementRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Copy chosen fields from the tenant's record into the agreement details",
)
def apply_prefill(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, body: PrefillApply
) -> AdminAgreementRead:
    """The contract keeps its own copy; later tenant edits do not change it."""
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    service = AgreementService(session, admin)
    service.apply_prefill(contract, body.keys)
    return admin_agreement_read(service, contract)


@router.put(
    "/contracts/{contract_id}/agreement/details",
    response_model=AdminAgreementRead,
    responses={**NOT_FOUND, **CONFLICT, 400: _error("A value is not valid.")},
    summary="Save the agreement's customer details",
)
def save_details(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, body: AgreementDetails
) -> AdminAgreementRead:
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    service = AgreementService(session, admin)
    service.save_details(contract, body.values)
    return admin_agreement_read(service, contract)


@router.put(
    "/contracts/{contract_id}/agreement/fields",
    response_model=AdminAgreementRead,
    responses={**NOT_FOUND, **CONFLICT, 400: _error("A value is not valid.")},
    summary="Save the template's own fields and other charges for this contract",
)
def save_contract_fields(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, body: ContractFieldsInput
) -> AdminAgreementRead:
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    service = AgreementService(session, admin)
    service.save_contract_fields(contract, body.values, admin=True)
    return admin_agreement_read(service, contract)


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
    """Allowed while the contract is PENDING or ACTIVE. Never approves or activates the
    contract and holds no space; activation stays a separate action."""
    contract = require(session, TenantOccupancy, contract_id, "Contract")
    return DocumentRead.model_validate(
        AgreementService(session, admin, storage).generate(contract, admin=True)
    )


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
    # Retired in Phase 4B: always 409, asking for the signed copy (see /signed-copy).
    ("sign", DocumentStatus.SIGNED, "Retired: upload the signed copy instead"),
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
    document = require(session, ContractDocument, document_id, "Document")
    return LinkRead(
        url=AgreementService(session, admin, storage).document_link(document), expires_in=300
    )


_SIGNERS = TypeAdapter(list[Signer])


def _signed_copy(
    admin, session, storage, document_id, file, signed_on, signers, notes, reason
) -> SignedCopyRead:  # type: ignore[no-untyped-def]
    try:
        people = _SIGNERS.validate_json(signers or "[]")
    except ValidationError as exc:
        raise BadRequestError(
            "signers must be a JSON list of {party: TENANT or COMPANY, name, designation}."
        ) from exc
    data = file.file.read(MAX_SIGNED_COPY_BYTES + 1)
    if not data:
        raise BadRequestError("The file is empty.")
    copy = AgreementService(session, admin, storage).upload_signed_copy(
        document_id,
        data=data,
        file_name=file.filename or "signed.pdf",
        signed_on=signed_on,
        signers=[p.model_dump() for p in people],
        notes=notes,
        replace_reason=reason,
    )
    return SignedCopyRead.model_validate(copy)


@router.post(
    "/contract-documents/{document_id}/signed-copy",
    response_model=SignedCopyRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, 400: _error("Not a PDF, too large, or a detail missing.")},
    summary="Upload the paper-signed agreement (PDF); the document becomes SIGNED",
)
def upload_signed_copy(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    document_id: uuid.UUID,
    file: UploadFile,
    signed_on: Annotated[date, Form()],
    signers: Annotated[
        str, Form(description='JSON: [{"party": "TENANT", "name": "...", "designation": "..."}]')
    ],
    notes: Annotated[str | None, Form(max_length=2000)] = None,
) -> SignedCopyRead:
    """A scan of a paper signature, not a verified digital signature. At most 20 MB; the
    file must be a PDF. The contract is not activated by this; activation stays separate."""
    return _signed_copy(admin, session, storage, document_id, file, signed_on, signers, notes, None)


@router.post(
    "/contract-documents/{document_id}/signed-copy/replace",
    response_model=SignedCopyRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, 400: _error("Not a PDF, too large, or a detail missing.")},
    summary="Replace the signed copy; the old one is kept with the reason",
)
def replace_signed_copy(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    document_id: uuid.UUID,
    file: UploadFile,
    signed_on: Annotated[date, Form()],
    signers: Annotated[str, Form()],
    reason: Annotated[str, Form(min_length=1, max_length=1000)],
    notes: Annotated[str | None, Form(max_length=2000)] = None,
) -> SignedCopyRead:
    return _signed_copy(
        admin, session, storage, document_id, file, signed_on, signers, notes, reason
    )


@router.get(
    "/contract-documents/{document_id}/signed-copies",
    response_model=list[SignedCopyRead],
    responses=NOT_FOUND,
    summary="Every signed copy of a document, newest first (replaced ones included)",
)
def list_signed_copies(
    admin: CurrentAdmin, session: DbSession, document_id: uuid.UUID
) -> list[SignedCopyRead]:
    require(session, ContractDocument, document_id, "Document")
    return [
        SignedCopyRead.model_validate(c)
        for c in AgreementService(session, admin).signed_copies(document_id)
    ]


@router.get(
    "/contract-signed-copies/{copy_id}/download",
    response_model=LinkRead,
    responses=NOT_FOUND,
    summary="A short-lived link to a signed copy",
)
def download_signed_copy(
    admin: CurrentAdmin, session: DbSession, storage: Storage, copy_id: uuid.UUID
) -> LinkRead:
    return LinkRead(
        url=AgreementService(session, admin, storage).signed_copy_link(copy_id), expires_in=300
    )
