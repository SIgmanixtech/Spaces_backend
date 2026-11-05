"""Contract agreements: template management, template selection, customer details, and
document generation from admin-approved Word templates.

Rules this module keeps:

* A contract's type (SPACE_LEASE or COWORKING_LICENCE) comes from the contract itself or,
  when unset there, from its space. Both are set by an administrator. If neither is set,
  or they disagree, nothing is generated and the reason is reported.
* Only an ACTIVE (approved) template of exactly that type is used: the property's own,
  else the default for every property. A template can be approved only when every
  placeholder in it is one this module knows for its type.
* Every value put into a document comes from the database (contract, property, space,
  booking, prices) or from details the customer saved and this module validated. A
  placeholder with no value stops generation with a list of what is missing; nothing is
  filled with a guess.
* Generation needs a contract the property team has approved (ACTIVE). It is idempotent:
  the same inputs return the same document. Bytes are stored before the row is written,
  and removed again if the row cannot be saved, so a failure never looks like success.
* Documents are GENERATED, then APPROVED, SENT_FOR_SIGNATURE and SIGNED only by explicit
  administrator actions. A signed agreement is never replaced.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.agreement import ContractDocument, ContractSignedCopy, ContractTemplate
from app.models.booking import Booking
from app.models.building import Building, Floor
from app.models.company import CompanyProfile
from app.models.crm import Proposal
from app.models.enums import ContractStatus, ContractType, DocumentStatus, TemplateStatus
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.services.amount_words import rupees_in_words
from app.services.base import commit, flush, log_activity, require
from app.services.contracts import group_members
from app.services.occupancy import business_today
from app.storage import ObjectStorage, StorageError

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_TEMPLATE_BYTES = 5 * 1024 * 1024
PDF = "application/pdf"
# Signed copies (Phase 4B): a scan of the agreement signed on paper.
MAX_SIGNED_COPY_BYTES = 20 * 1024 * 1024
SIGNER_PARTIES = ("TENANT", "COMPANY")
CUSTOM_FIELD_TYPES = ("text", "number", "date")
# Kept on the contract with the template's own fields, but not one of them.
RESERVED_FIELDS = ("other_charges",)
COMPANY_KEYS = (
    "legal_name",
    "registered_address",
    "signatory_name",
    "signatory_designation",
    "signatory_address",
    "gstin",
    "pan",
    "cin",
)
# Tenant record -> agreement customer field, for the admin's explicit prefill.
TENANT_PREFILL = {
    "address": "registered_address",
    "representative_name": "signatory_name",
    "representative_designation": "signatory_designation",
    "signatory_address": "signatory_address",
    "gstin": "gstin",
    "pan": "pan",
    "cin": "cin",
}
TYPE_LABEL = {
    ContractType.SPACE_LEASE: "Space-based rental / lease contract",
    ContractType.COWORKING_LICENCE: "Coworking space licence agreement",
}

# --- what a customer may be asked for ---------------------------------------------------------


@dataclass(frozen=True)
class CustomerField:
    key: str
    label: str
    max_length: int
    pattern: str | None = None
    choices: tuple[str, ...] = ()
    hint: str = ""


CUSTOMER_FIELDS: dict[str, CustomerField] = {
    f.key: f
    for f in (
        CustomerField(
            "entity_type", "Individual or company", 20, choices=("INDIVIDUAL", "COMPANY")
        ),
        CustomerField("legal_name", "Legal name", 200),
        CustomerField("company_name", "Registered business name", 200),
        CustomerField("address", "Registered or correspondence address", 500),
        CustomerField("representative_name", "Authorised representative", 200),
        CustomerField("representative_designation", "Representative's designation", 120),
        CustomerField("phone", "Contact phone", 32, pattern=r"^\+?[0-9 ()-]{7,20}$"),
        CustomerField("email", "Contact email", 320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
        CustomerField(
            "gstin",
            "GSTIN",
            15,
            pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$",
            hint="15 characters, e.g. 36ABCDE1234F1Z5",
        ),
        CustomerField("pan", "PAN", 10, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$", hint="e.g. ABCDE1234F"),
        CustomerField(
            "cin",
            "CIN",
            21,
            pattern=r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$",
            hint="21 characters, e.g. U72200TG2015PTC123456",
        ),
        CustomerField("signatory_address", "Signatory's address (if different)", 500),
    )
}

# --- what a template may contain ---------------------------------------------------------------

COMMON_PLACEHOLDERS = {
    "contract.reference": "Contract reference",
    "contract.type": "Contract type",
    "contract.start_date": "Start date",
    "contract.end_date": "End date",
    "contract.terms": "Agreed terms (from the contract)",
    "booking.reference": "Booking reference",
    "tenant.name": "Tenant name on record",
    "property.name": "Property name",
    "property.address": "Property address",
    "property.city": "Property city",
    "building.name": "Building",
    "floor.name": "Floor",
    "space.name": "Space",
    "space.type": "Space type",
    "space.area_sqft": "Space area (sq ft)",
    "space.capacity": "Space capacity",
    "pricing.amount": "Approved monthly amount",
    "pricing.billing_period": "Billing period",
    "pricing.deposit": "Security deposit",
    "pricing.currency": "Currency",
    "document.generated_on": "Date generated",
    # Phase 4B
    "pricing.amount_words": "Approved monthly amount in words",
    "pricing.deposit_words": "Security deposit in words",
    "contract.other_charges": "Other charges (as entered)",
    "proposal.reference": "Proposal reference",
    "proposal.accepted_on": "Proposal accepted on",
    "company.legal_name": "Company legal name",
    "company.registered_address": "Company registered address",
    "company.signatory_name": "Company signatory",
    "company.signatory_designation": "Company signatory's designation",
    "company.signatory_address": "Company signatory's address",
    "company.gstin": "Company GSTIN",
    "company.pan": "Company PAN",
    "company.cin": "Company CIN",
}
# Each type has its own vocabulary; a licence template cannot use lease terms or vice versa.
TYPE_PLACEHOLDERS = {
    ContractType.SPACE_LEASE: {
        "lease.premises": "Leased premises",
        "lease.rent": "Monthly rent",
        "lease.commencement_date": "Lease commencement date",
        "lease.expiry_date": "Lease expiry date",
        "lease.rent_words": "Monthly rent in words",
    },
    ContractType.COWORKING_LICENCE: {
        "licence.workspace": "Licensed workspace",
        "licence.fee": "Licence fee",
        "licence.commencement_date": "Licence commencement date",
        "licence.expiry_date": "Licence expiry date",
        "licence.fee_words": "Licence fee in words",
    },
}

_EXPR = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.S)
_NAME = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")


def allowed_placeholders(
    contract_type: ContractType, custom_fields: list[dict[str, Any]] | None = None
) -> dict[str, str]:
    customer = {f"customer.{k}": f.label for k, f in CUSTOMER_FIELDS.items()}
    custom = {f"custom.{f['key']}": f["label"] for f in custom_fields or []}
    return {**COMMON_PLACEHOLDERS, **TYPE_PLACEHOLDERS[contract_type], **customer, **custom}


def read_placeholders(data: bytes) -> list[str]:
    """Every {{ ... }} expression in the document body, headers and footers, as written
    (a filter such as |upper is ignored). Raises BadRequestError for a non-DOCX file."""
    from docxtpl import DocxTemplate

    if not data.startswith(b"PK"):
        raise BadRequestError("Upload a Word document (.docx).")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise BadRequestError("Upload a Word document (.docx).")
        template = DocxTemplate(io.BytesIO(data))
        template.init_docx()
        parts = [template.get_xml()]
        for rel in template.docx.part.rels.values():
            if rel.reltype.endswith(("/header", "/footer")):
                parts.append(rel.target_part.blob.decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BadRequestError("The file is not a readable Word document.") from exc
    found: list[str] = []
    for xml in parts:
        text = re.sub(r"<[^>]+>", "", template.patch_xml(xml))
        for expression in _EXPR.findall(text):
            name = expression.split("|", 1)[0].strip()
            if name not in found:
                found.append(name)
    return found


def unknown_placeholders(
    names: list[str],
    contract_type: ContractType,
    custom_fields: list[dict[str, Any]] | None = None,
) -> list[str]:
    allowed = allowed_placeholders(contract_type, custom_fields)
    return [n for n in names if not _NAME.match(n) or n not in allowed]


_KEY = re.compile(r"^[a-z][a-z_]{0,39}$")


def clean_custom_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A template's own fields (Phase 4B): key, label, type (text, number or date) and
    required. Filled as {{ custom.<key> }}. Raises BadRequestError with every problem."""
    errors: list[dict[str, str]] = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if len(fields) > 50:
        raise BadRequestError("A template has at most 50 custom fields.")
    for i, raw in enumerate(fields):
        key = str(raw.get("key") or "").strip()
        label = " ".join(str(raw.get("label") or "").split())
        kind = str(raw.get("type") or "text").strip().lower()
        where = f"custom_fields[{i}]"
        if not _KEY.match(key):
            errors.append({"field": where, "message": "Key: lowercase letters and _ only."})
        elif key in seen:
            errors.append({"field": where, "message": f"Key {key} is used twice."})
        elif key in RESERVED_FIELDS:
            errors.append({"field": where, "message": f"Key {key} is reserved."})
        if not label or len(label) > 120:
            errors.append({"field": where, "message": "Label: 1 to 120 characters."})
        if kind not in CUSTOM_FIELD_TYPES:
            errors.append({"field": where, "message": "Type: text, number or date."})
        seen.add(key)
        out.append(
            {"key": key, "label": label, "type": kind, "required": bool(raw.get("required"))}
        )
    if errors:
        raise BadRequestError("Some custom fields are not valid.", errors=errors)
    return out


def _custom_value(spec: dict[str, Any], raw: object) -> str | None:
    """Check one entered value against its field type; returns the stored text."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str | int | float):
        raise ValueError("Must be text.")
    value = " ".join(str(raw).split()) if spec["type"] != "text" else str(raw).strip()
    if spec["type"] == "number":
        try:
            Decimal(value.replace(",", ""))
        except ArithmeticError as exc:
            raise ValueError("Must be a number.") from exc
        value = value.replace(",", "")
    elif spec["type"] == "date":
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Must be a date (YYYY-MM-DD).") from exc
    if len(value) > 5000:
        raise ValueError("At most 5000 characters.")
    return value


# --- formatting -------------------------------------------------------------------------------


def _day(value: date | None) -> str | None:
    return value.strftime("%d %B %Y") if value else None


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    whole, _, cents = f"{Decimal(value):.2f}".partition(".")
    # Indian grouping: 1,15,500.00
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail]) + "." + cents


def _words(value: Decimal | None) -> str | None:
    return rupees_in_words(value) if value is not None else None


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for key, value in flat.items():
        here = nested
        *parents, last = key.split(".")
        for part in parents:
            here = here.setdefault(part, {})
        here[last] = value
    return nested


# --- the service -------------------------------------------------------------------------------


@dataclass
class TypeDecision:
    contract_type: ContractType | None
    source: str | None  # "contract" or "space"
    problem: str | None


class AgreementService:
    def __init__(
        self, session: Session, actor: AppUser | None, storage: ObjectStorage | None = None
    ) -> None:
        self.session = session
        self.actor = actor
        self.storage = storage

    # --- templates -------------------------------------------------------------------------

    def upload_template(
        self,
        *,
        contract_type: ContractType,
        name: str,
        data: bytes,
        original_name: str,
        property_id: uuid.UUID | None,
        required_fields: list[str],
        notes: str | None,
        custom_fields: list[dict[str, Any]] | None = None,
    ) -> ContractTemplate:
        custom = clean_custom_fields(custom_fields or [])
        if len(data) > MAX_TEMPLATE_BYTES:
            raise BadRequestError("The template is larger than 5 MB.")
        if property_id is not None:
            require(self.session, Property, property_id, "Property")
        unknown_fields = [f for f in required_fields if f not in CUSTOMER_FIELDS]
        if unknown_fields:
            raise BadRequestError(f"Unknown customer field(s): {', '.join(unknown_fields)}.")
        placeholders = read_placeholders(data)
        used = [n.split(".", 1)[1] for n in placeholders if n.startswith("customer.")]
        fields = list(dict.fromkeys([*required_fields, *(u for u in used if u in CUSTOMER_FIELDS)]))
        scope = (
            ContractTemplate.property_id == property_id
            if property_id
            else ContractTemplate.property_id.is_(None)
        )
        version = (
            self.session.scalar(
                select(func.max(ContractTemplate.version)).where(
                    ContractTemplate.contract_type == contract_type, scope
                )
            )
            or 0
        ) + 1
        template = ContractTemplate(
            id=uuid.uuid4(),
            contract_type=contract_type,
            property_id=property_id,
            name=name.strip(),
            version=version,
            status=TemplateStatus.DRAFT,
            bucket=self._storage().bucket,
            original_name=(original_name or "template.docx")[-255:],
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            placeholders=placeholders,
            required_fields=fields,
            notes=notes,
            custom_fields=custom,
            uploaded_by=self.actor.id if self.actor else None,
        )
        template.object_path = f"contracts/templates/{template.id}.docx"
        self._storage().put(template.object_path, data, DOCX)
        try:
            self.session.add(template)
            flush(self.session)
            self._log(
                "uploaded",
                "contract_templates",
                template.id,
                template.property_id,
                f'Contract template "{template.name}" v{version} uploaded.',
            )
            commit(self.session)
        except BaseException:
            self.session.rollback()
            self._discard(template.object_path)
            raise
        return template

    def approve_template(self, template_id: uuid.UUID) -> ContractTemplate:
        """Approve a DRAFT and make it the ACTIVE template for its type and scope; the
        previously active one is retired (it stays, with every document made from it)."""
        template = self._template(template_id)
        if template.status != TemplateStatus.DRAFT:
            raise ConflictError(f"A {template.status.lower()} template cannot be approved.")
        unknown = unknown_placeholders(
            template.placeholders, ContractType(template.contract_type), template.custom_fields
        )
        if unknown:
            raise ConflictError(
                "The template uses placeholders this system does not fill: "
                + ", ".join(unknown)
                + ". Correct the document and upload it again."
            )
        now = datetime.now(UTC)
        scope = (
            ContractTemplate.property_id == template.property_id
            if template.property_id
            else ContractTemplate.property_id.is_(None)
        )
        for current in self.session.scalars(
            select(ContractTemplate).where(
                ContractTemplate.contract_type == template.contract_type,
                ContractTemplate.status == TemplateStatus.ACTIVE,
                scope,
            )
        ):
            current.status = TemplateStatus.RETIRED
            current.retired_at = now
        flush(self.session)
        template.status = TemplateStatus.ACTIVE
        template.approved_by = self.actor.id if self.actor else None
        template.approved_at = now
        self._log(
            "approved",
            "contract_templates",
            template.id,
            template.property_id,
            f'Contract template "{template.name}" v{template.version} approved and active.',
        )
        commit(self.session)
        return template

    def set_custom_fields(
        self, template_id: uuid.UUID, fields: list[dict[str, Any]]
    ) -> ContractTemplate:
        """A draft's own fields (Phase 4B). An approved template's fields are fixed, so
        documents already made from it stay explainable; upload a new version instead."""
        template = self._template(template_id)
        if template.status != TemplateStatus.DRAFT:
            raise ConflictError(
                "Only a draft template's custom fields can change. Upload a new version."
            )
        template.custom_fields = clean_custom_fields(fields)
        self._log(
            "custom_fields_changed",
            "contract_templates",
            template.id,
            template.property_id,
            f'Contract template "{template.name}" v{template.version}: custom fields set '
            f"({len(template.custom_fields)}).",
        )
        commit(self.session)
        return template

    def retire_template(self, template_id: uuid.UUID) -> ContractTemplate:
        template = self._template(template_id)
        if template.status == TemplateStatus.RETIRED:
            raise ConflictError("The template is already retired.")
        if template.status == TemplateStatus.DRAFT:
            template.approved_at = datetime.now(UTC)  # a draft is retired without use
        template.status = TemplateStatus.RETIRED
        template.retired_at = datetime.now(UTC)
        self._log(
            "retired",
            "contract_templates",
            template.id,
            template.property_id,
            f'Contract template "{template.name}" v{template.version} retired.',
        )
        commit(self.session)
        return template

    def templates(
        self, contract_type: ContractType | None = None, status: TemplateStatus | None = None
    ) -> list[ContractTemplate]:
        statement = select(ContractTemplate).order_by(
            ContractTemplate.contract_type,
            ContractTemplate.property_id,
            ContractTemplate.version.desc(),
        )
        if contract_type:
            statement = statement.where(ContractTemplate.contract_type == contract_type)
        if status:
            statement = statement.where(ContractTemplate.status == status)
        return list(self.session.scalars(statement))

    def template_link(self, template_id: uuid.UUID, expires_in: int = 600) -> str:
        return self._storage().signed_url(self._template(template_id).object_path, expires_in).url

    # --- type and template for a contract ------------------------------------------------

    def decide_type(self, contract: TenantOccupancy) -> TypeDecision:
        space = self.session.get(Space, contract.space_id) if contract.space_id else None
        own = ContractType(contract.contract_type) if contract.contract_type else None
        from_space = (
            ContractType(space.contract_type) if space is not None and space.contract_type else None
        )
        if own and from_space and own != from_space:
            return TypeDecision(
                None,
                None,
                f"The contract is set to {TYPE_LABEL[own].lower()} but its space is set to "
                f"{TYPE_LABEL[from_space].lower()}. The property team must resolve this.",
            )
        if own:
            return TypeDecision(own, "contract", None)
        if from_space:
            return TypeDecision(from_space, "space", None)
        return TypeDecision(
            None,
            None,
            "The contract type (space lease or coworking licence) has not been set by the "
            "property team yet.",
        )

    def select_template(
        self, contract_type: ContractType, property_id: uuid.UUID
    ) -> ContractTemplate | None:
        for scope in (
            ContractTemplate.property_id == property_id,
            ContractTemplate.property_id.is_(None),
        ):
            found = self.session.scalar(
                select(ContractTemplate).where(
                    ContractTemplate.contract_type == contract_type,
                    ContractTemplate.status == TemplateStatus.ACTIVE,
                    scope,
                )
            )
            if found is not None:
                return found
        return None

    def plan(self, contract: TenantOccupancy, *, admin: bool = False) -> dict[str, Any]:
        """Where the agreement for a contract stands: type, template, fields, problems.

        `admin` (Phase 4B, Option A): the property team may generate while the contract is
        still PENDING; generating never approves, activates or holds anything. Customers
        (admin=False) still need an approved (ACTIVE) contract, as before."""
        decision = self.decide_type(contract)
        template = (
            self.select_template(decision.contract_type, contract.property_id)
            if decision.contract_type
            else None
        )
        problem = decision.problem
        if problem is None and template is None:
            problem = (
                f"No approved {TYPE_LABEL[decision.contract_type].lower()} template is active "
                "yet. The property team must upload and approve one."
            )
        saved = contract.customer_details or {}
        suggested = self._suggestions(contract)
        fields = [
            {
                "key": key,
                "label": CUSTOMER_FIELDS[key].label,
                "hint": CUSTOMER_FIELDS[key].hint,
                "choices": list(CUSTOMER_FIELDS[key].choices),
                "value": saved.get(key),
                "suggested": None if saved.get(key) else suggested.get(key),
            }
            for key in (template.required_fields if template else [])
        ]
        missing: list[str] = []
        if template is not None and problem is None:
            values, missing = self._values(contract, template)
        allowed = (
            (ContractStatus.PENDING, ContractStatus.ACTIVE) if admin else (ContractStatus.ACTIVE,)
        )
        if problem is None and contract.status not in allowed:
            problem = (
                f"A {contract.status.lower()} contract's agreement is not generated."
                if admin
                else "The property team must approve the contract before its agreement is "
                "generated."
            )
        return {
            "contract_type": decision.contract_type,
            "contract_type_label": TYPE_LABEL.get(decision.contract_type)
            if decision.contract_type
            else None,
            "type_source": decision.source,
            "template": template,
            "fields": fields,
            "missing": missing,
            "problem": problem,
            "can_generate": problem is None and not missing,
        }

    # --- customer details ------------------------------------------------------------------

    def save_details(self, contract: TenantOccupancy, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and keep the customer's details. Only known fields are accepted."""
        errors = []
        clean: dict[str, Any] = {}
        for key, raw in values.items():
            spec = CUSTOMER_FIELDS.get(key)
            if spec is None:
                errors.append({"field": key, "message": "Not a field this agreement uses."})
                continue
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                clean[key] = None
                continue
            if not isinstance(raw, str):
                errors.append({"field": key, "message": "Must be text."})
                continue
            multiline = key in ("address", "signatory_address")
            value = raw.strip() if multiline else " ".join(raw.split())
            if key in ("gstin", "pan", "cin"):
                value = value.upper().replace(" ", "")
            if key == "email":
                value = value.lower()
            if len(value) > spec.max_length:
                errors.append({"field": key, "message": f"At most {spec.max_length} characters."})
            elif spec.choices and value not in spec.choices:
                errors.append({"field": key, "message": f"One of {', '.join(spec.choices)}."})
            elif spec.pattern and not re.match(spec.pattern, value):
                errors.append({"field": key, "message": f"Not a valid {spec.label}."})
            else:
                clean[key] = value
        if errors:
            raise BadRequestError("Some details are not valid.", errors=errors)
        if any(d.status == DocumentStatus.SIGNED for d in self._documents(contract.id)):
            raise ConflictError("The agreement is signed; its details can no longer change.")
        merged = {**(contract.customer_details or {}), **clean}
        contract.customer_details = {k: v for k, v in merged.items() if v is not None}
        commit(self.session)
        return contract.customer_details

    # --- generation ------------------------------------------------------------------------

    def generate(self, contract: TenantOccupancy, *, admin: bool = False) -> ContractDocument:
        """Fill the template; never approves or activates the contract, holds no space."""
        plan = self.plan(contract, admin=admin)
        if plan["problem"]:
            raise ConflictError(plan["problem"])
        template: ContractTemplate = plan["template"]
        values, missing = self._values(contract, template)
        if missing:
            raise BadRequestError(
                "These details are needed before the agreement can be generated: "
                + ", ".join(missing)
                + ".",
                errors=[{"field": m, "message": "Required"} for m in missing],
            )
        documents = self._documents(contract.id)
        if any(d.status == DocumentStatus.SIGNED for d in documents):
            raise ConflictError("This agreement is already signed; a new one is not generated.")
        input_hash = hashlib.sha256(
            json.dumps(
                {"template": str(template.id), "values": values}, sort_keys=True, default=str
            ).encode()
        ).hexdigest()
        for document in documents:
            if document.input_hash == input_hash and document.status != DocumentStatus.SUPERSEDED:
                return document  # same inputs: the same document, nothing new is made

        from docxtpl import DocxTemplate
        from jinja2 import Environment, StrictUndefined

        storage = self._storage()
        try:
            source = storage.get(template.object_path)
        except StorageError as exc:
            raise ConflictError("The template file could not be read. Try again later.") from exc
        try:
            rendered = DocxTemplate(io.BytesIO(source))
            rendered.render(_nest(values), Environment(undefined=StrictUndefined), autoescape=True)
            out = io.BytesIO()
            rendered.save(out)
        except Exception as exc:  # the template is checked on approval; anything else is a fault
            raise ConflictError(
                "The agreement could not be generated from the template. The property team "
                "has to check the template."
            ) from exc
        data = out.getvalue()
        document = ContractDocument(
            id=uuid.uuid4(),
            contract_id=contract.id,
            template_id=template.id,
            template_version=template.version,
            contract_type=template.contract_type,
            status=DocumentStatus.GENERATED,
            bucket=storage.bucket,
            file_name=f"{contract.reference}-{template.contract_type.lower()}-v{template.version}.docx",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            input_hash=input_hash,
            data=values,
            generated_by=self.actor.id if self.actor else None,
        )
        document.object_path = f"contracts/documents/{contract.id}/{document.id}.docx"
        storage.put(document.object_path, data, DOCX)
        try:
            now = datetime.now(UTC)
            for older in documents:
                if older.status in (
                    DocumentStatus.GENERATED,
                    DocumentStatus.APPROVED,
                    DocumentStatus.SENT_FOR_SIGNATURE,
                ):
                    older.status = DocumentStatus.SUPERSEDED
                    older.superseded_at = now
            self.session.add(document)
            flush(self.session)
            self._log(
                "generated",
                "contract_documents",
                document.id,
                contract.property_id,
                f"Agreement for {contract.reference} generated from template v{template.version}.",
            )
            commit(self.session)
        except BaseException:
            self.session.rollback()
            self._discard(document.object_path)
            raise
        return document

    def advance(self, document_id: uuid.UUID, to: DocumentStatus) -> ContractDocument:
        """An administrator moves a document one step: approve, send, or record signing."""
        document = self.session.get(ContractDocument, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        if to == DocumentStatus.SIGNED:
            # Phase 4B: a signature is recorded only with its evidence.
            raise ConflictError(
                "Upload the signed copy (PDF) to record the signature; it can no longer be "
                "marked signed without it."
            )
        allowed = {
            DocumentStatus.APPROVED: DocumentStatus.GENERATED,
            DocumentStatus.SENT_FOR_SIGNATURE: DocumentStatus.APPROVED,
            DocumentStatus.SIGNED: DocumentStatus.SENT_FOR_SIGNATURE,
        }
        if allowed.get(to) != document.status:
            raise ConflictError(
                f"A {document.status.lower().replace('_', ' ')} document cannot be marked "
                f"{to.lower().replace('_', ' ')}."
            )
        now = datetime.now(UTC)
        document.status = to
        if to == DocumentStatus.APPROVED:
            document.approved_at = now
        elif to == DocumentStatus.SENT_FOR_SIGNATURE:
            document.sent_at = now
        else:
            document.signed_at = now
        contract = self.session.get(TenantOccupancy, document.contract_id)
        self._log(
            to.lower(),
            "contract_documents",
            document.id,
            contract.property_id if contract else None,
            f"Agreement {document.file_name} marked {to.lower().replace('_', ' ')}.",
        )
        commit(self.session)
        return document

    def documents(self, contract_id: uuid.UUID) -> list[ContractDocument]:
        return self._documents(contract_id)

    # --- Phase 4B: prefill, contract fields, signed copies -----------------------------------

    def _signed(self, contract: TenantOccupancy) -> bool:
        return any(d.status == DocumentStatus.SIGNED for d in self._documents(contract.id))

    def prefill_preview(self, contract: TenantOccupancy) -> list[dict[str, Any]]:
        """What the tenant's record would put in the agreement details, next to what is
        saved now. Nothing changes until the admin applies it."""
        tenant = self.session.get(Tenant, contract.tenant_id)
        saved = contract.customer_details or {}
        source: dict[str, str | None] = {
            "legal_name": (tenant.company_name or tenant.name) if tenant else None,
            "company_name": tenant.company_name if tenant else None,
            "entity_type": ("COMPANY" if tenant and tenant.company_name else None),
            "email": tenant.email if tenant else None,
            "phone": tenant.phone if tenant else None,
        }
        for key, column in TENANT_PREFILL.items():
            source[key] = getattr(tenant, column) if tenant else None
        out = []
        for key, value in source.items():
            current = saved.get(key)
            out.append(
                {
                    "key": key,
                    "label": CUSTOMER_FIELDS[key].label,
                    "current": current,
                    "source": value,
                    "differs": bool(value) and value != current,
                }
            )
        return out

    def apply_prefill(self, contract: TenantOccupancy, keys: list[str]) -> dict[str, Any]:
        """Copy the chosen fields from the tenant's record into this contract's own
        details (the contract keeps its copy; later tenant edits do not change it)."""
        preview = {row["key"]: row for row in self.prefill_preview(contract)}
        unknown = [k for k in keys if k not in preview]
        if unknown:
            raise BadRequestError(f"Not a prefill field: {', '.join(unknown)}.")
        values = {k: preview[k]["source"] for k in keys if preview[k]["source"]}
        if not values:
            raise BadRequestError("The tenant's record has nothing to copy for those fields.")
        saved = self.save_details(contract, values)
        self._log(
            "prefilled",
            "tenant_occupancies",
            contract.id,
            contract.property_id,
            f"Agreement details for {contract.reference} copied from the tenant's record: "
            + ", ".join(CUSTOMER_FIELDS[k].label for k in values)
            + ".",
        )
        commit(self.session)
        return saved

    def save_contract_fields(
        self, contract: TenantOccupancy, values: dict[str, Any], *, admin: bool = False
    ) -> dict[str, Any]:
        """Values for the applicable template's own fields, and other charges."""
        if self._signed(contract):
            raise ConflictError("The agreement is signed; its details can no longer change.")
        plan = self.plan(contract, admin=admin)
        template: ContractTemplate | None = plan["template"]
        specs = {f["key"]: f for f in (template.custom_fields if template else [])}
        specs["other_charges"] = {"key": "other_charges", "label": "Other charges", "type": "text"}
        errors = []
        clean: dict[str, Any] = {}
        for key, raw in values.items():
            spec = specs.get(key)
            if spec is None:
                errors.append({"field": key, "message": "Not a field of this agreement."})
                continue
            try:
                clean[key] = _custom_value(spec, raw)
            except ValueError as exc:
                errors.append({"field": key, "message": str(exc)})
        if errors:
            raise BadRequestError("Some contract fields are not valid.", errors=errors)
        merged = {**(contract.contract_fields or {}), **clean}
        contract.contract_fields = {k: v for k, v in merged.items() if v is not None}
        if clean:
            self._log(
                "contract_fields_saved",
                "tenant_occupancies",
                contract.id,
                contract.property_id,
                f"Contract fields for {contract.reference} saved: "
                + ", ".join(specs[k]["label"] for k in clean)
                + ".",
            )
        commit(self.session)
        return contract.contract_fields

    def signed_copies(self, document_id: uuid.UUID) -> list[ContractSignedCopy]:
        return list(
            self.session.scalars(
                select(ContractSignedCopy)
                .where(ContractSignedCopy.document_id == document_id)
                .order_by(ContractSignedCopy.created_at.desc(), ContractSignedCopy.id)
            )
        )

    def current_copies(self, document_ids: set[uuid.UUID]) -> dict[uuid.UUID, ContractSignedCopy]:
        if not document_ids:
            return {}
        return {
            c.document_id: c
            for c in self.session.scalars(
                select(ContractSignedCopy).where(
                    ContractSignedCopy.document_id.in_(document_ids),
                    ContractSignedCopy.replaced_at.is_(None),
                )
            )
        }

    def upload_signed_copy(
        self,
        document_id: uuid.UUID,
        *,
        data: bytes,
        file_name: str,
        signed_on: date,
        signers: list[dict[str, Any]],
        notes: str | None,
        replace_reason: str | None = None,
    ) -> ContractSignedCopy:
        """Store the scan of the paper-signed agreement and mark the document SIGNED.
        Evidence of a paper signature, not a verified digital signature. Never activates
        the contract. A current copy is replaced only with a reason; the old one stays."""
        document = self.session.scalar(
            select(ContractDocument).where(ContractDocument.id == document_id).with_for_update()
        )
        if document is None:
            raise NotFoundError("Document not found.")
        if len(data) > MAX_SIGNED_COPY_BYTES:
            raise BadRequestError("The signed copy is larger than 20 MB.")
        if not data.startswith(b"%PDF-"):
            raise BadRequestError("Upload the signed copy as a PDF file.")
        # The business date (Asia/Kolkata), not UTC: early-morning IST is still "yesterday"
        # in UTC, which would refuse a document signed today.
        if signed_on > business_today():
            raise BadRequestError("The signing date cannot be in the future.")
        clean_signers = []
        for i, raw in enumerate(signers):
            party = str(raw.get("party") or "").upper()
            name = " ".join(str(raw.get("name") or "").split())
            designation = " ".join(str(raw.get("designation") or "").split()) or None
            if party not in SIGNER_PARTIES or not name or len(name) > 200:
                raise BadRequestError(
                    f"Signer {i + 1}: give the party (tenant or company) and a name."
                )
            clean_signers.append({"party": party, "name": name, "designation": designation})
        if not clean_signers:
            raise BadRequestError("Give at least one signer.")
        current = self.current_copies({document.id}).get(document.id)
        reason = (replace_reason or "").strip() or None
        if current is not None and reason is None:
            raise ConflictError(
                "This agreement already has a signed copy. Replacing it needs a reason; the "
                "current copy is kept."
            )
        if current is None and reason is not None:
            raise BadRequestError("There is no signed copy to replace.")
        if current is None and document.status not in (
            DocumentStatus.APPROVED,
            DocumentStatus.SENT_FOR_SIGNATURE,
            DocumentStatus.SIGNED,  # signed before copies were kept: add its evidence
        ):
            raise ConflictError(
                f"A {document.status.lower().replace('_', ' ')} document cannot be signed. "
                "Approve it first."
            )
        storage = self._storage()
        copy = ContractSignedCopy(
            id=uuid.uuid4(),
            document_id=document.id,
            contract_id=document.contract_id,
            bucket=storage.bucket,
            file_name=(file_name or "signed.pdf")[-255:],
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            signed_on=signed_on,
            signers=clean_signers,
            notes=(notes or "").strip() or None,
            uploaded_by=self.actor.id if self.actor else None,
        )
        copy.object_path = f"contracts/signed/{document.contract_id}/{copy.id}.pdf"
        storage.put(copy.object_path, data, PDF)
        contract = self.session.get(TenantOccupancy, document.contract_id)
        try:
            now = datetime.now(UTC)
            if current is not None:
                current.replaced_at = now
                current.replace_reason = reason
                flush(self.session)
            first_signature = document.status != DocumentStatus.SIGNED
            document.status = DocumentStatus.SIGNED
            if first_signature or document.signed_at is None:
                document.signed_at = now
            self.session.add(copy)
            flush(self.session)
            names = ", ".join(s["name"] for s in clean_signers)
            self._log(
                "signed_copy_replaced" if current else "signed_copy_uploaded",
                "contract_documents",
                document.id,
                contract.property_id if contract else None,
                (
                    f"Signed copy of {document.file_name} replaced. Reason: {reason}"
                    if current
                    else f"Signed copy of {document.file_name} uploaded (signed on "
                    f"{signed_on.isoformat()} by {names}); the agreement is recorded as "
                    "signed on paper."
                ),
            )
            commit(self.session)
        except BaseException:
            self.session.rollback()
            self._discard(copy.object_path)
            raise
        return copy

    def signed_copy_link(self, copy_id: uuid.UUID, expires_in: int = 300) -> str:
        copy = self.session.get(ContractSignedCopy, copy_id)
        if copy is None:
            raise NotFoundError("Signed copy not found.")
        return self._storage().signed_url(copy.object_path, expires_in).url

    def document_link(self, document: ContractDocument, expires_in: int = 300) -> str:
        return self._storage().signed_url(document.object_path, expires_in).url

    # --- helpers -----------------------------------------------------------------------------

    def _values(
        self, contract: TenantOccupancy, template: ContractTemplate
    ) -> tuple[dict[str, Any], list[str]]:
        """The value of every placeholder the template uses, and the labels of any
        placeholder without a value."""
        available = self._facts(contract, ContractType(template.contract_type))
        details = contract.customer_details or {}
        for key in CUSTOMER_FIELDS:
            available[f"customer.{key}"] = details.get(key)
        entered = contract.contract_fields or {}
        optional: set[str] = set()
        for spec in template.custom_fields or []:
            name = f"custom.{spec['key']}"
            available[name] = entered.get(spec["key"])
            if not spec.get("required"):
                optional.add(name)
        labels = allowed_placeholders(ContractType(template.contract_type), template.custom_fields)
        values: dict[str, Any] = {}
        missing: list[str] = []
        for name in template.placeholders:
            value = available.get(name)
            if value is None or value == "":
                if name in optional:
                    values[name] = ""  # an optional field left empty prints nothing
                else:
                    missing.append(labels.get(name, name))
            else:
                values[name] = value
        for spec in template.custom_fields or []:
            if (
                spec.get("required")
                and not entered.get(spec["key"])
                and spec["label"] not in missing
            ):
                missing.append(spec["label"])
        for key in template.required_fields:
            if not details.get(key) and CUSTOMER_FIELDS[key].label not in missing:
                missing.append(CUSTOMER_FIELDS[key].label)
        return values, missing

    def _facts(self, contract: TenantOccupancy, contract_type: ContractType) -> dict[str, Any]:
        prop = self.session.get(Property, contract.property_id)
        space = self.session.get(Space, contract.space_id) if contract.space_id else None
        floor = self.session.get(Floor, space.floor_id) if space and space.floor_id else None
        building = (
            self.session.get(Building, space.building_id) if space and space.building_id else None
        )
        booking = self.session.get(Booking, contract.booking_id) if contract.booking_id else None
        tenant = self.session.get(Tenant, contract.tenant_id)
        address = (
            ", ".join(p for p in (prop.address, prop.city, prop.state, prop.postal_code) if p)
            if prop
            else None
        )
        members = group_members(self.session, contract)
        seat_names = (
            [
                s.name
                for s in self.session.scalars(
                    select(Space)
                    .where(Space.id.in_({m.space_id for m in members if m.space_id}))
                    .order_by(Space.name)
                )
            ]
            if len(members) > 1
            else []
        )
        rents = [m.monthly_rent for m in members]
        monthly_rent = (
            sum(rents) if all(r is not None for r in rents) else None  # type: ignore[misc]
        )
        # A group's deposit is the sum of the deposits entered on its seats (a contract made
        # from a proposal carries it on its first seat). A single contract: its own.
        deposits = [m.security_deposit for m in members if m.security_deposit is not None]
        deposit = sum(deposits) if deposits else None
        premises = ", ".join(
            p
            for p in (
                ", ".join(seat_names)
                if seat_names
                else (space.name if space else "The whole property"),
                floor.name if floor else None,
                building.name if building else None,
                prop.name if prop else None,
            )
            if p
        )
        facts: dict[str, Any] = {
            "contract.reference": contract.reference,
            "contract.type": TYPE_LABEL[contract_type],
            "contract.start_date": _day(contract.start_date),
            "contract.end_date": _day(contract.end_date),
            "contract.terms": contract.terms,
            "booking.reference": booking.reference if booking else None,
            "tenant.name": tenant.name if tenant else None,
            "property.name": prop.name if prop else None,
            "property.address": address,
            "property.city": prop.city if prop else None,
            "building.name": building.name if building else None,
            "floor.name": floor.name if floor else None,
            "space.name": space.name if space else "The whole property",
            "space.type": space.space_type.replace("_", " ").title() if space else "Property",
            "space.area_sqft": f"{space.area_sqft:,}" if space and space.area_sqft else None,
            "space.capacity": str(space.capacity) if space and space.capacity else None,
            "pricing.amount": _money(monthly_rent),
            "pricing.billing_period": "Monthly" if monthly_rent is not None else None,
            "pricing.deposit": _money(deposit),  # type: ignore[arg-type]
            "pricing.amount_words": _words(monthly_rent),  # type: ignore[arg-type]
            "pricing.deposit_words": _words(deposit),  # type: ignore[arg-type]
            "pricing.currency": contract.currency,
            "document.generated_on": _day(business_today()),  # IST date, not UTC
            "contract.other_charges": (contract.contract_fields or {}).get("other_charges"),
        }
        proposal = (
            self.session.get(Proposal, contract.proposal_id) if contract.proposal_id else None
        )
        facts["proposal.reference"] = proposal.reference if proposal else None
        facts["proposal.accepted_on"] = _day(proposal.accepted_on) if proposal else None
        # The company profile of the property, copied into the document when it is made.
        profile = (
            self.session.get(CompanyProfile, prop.company_profile_id)
            if prop and prop.company_profile_id
            else None
        )
        for key in COMPANY_KEYS:
            facts[f"company.{key}"] = getattr(profile, key) if profile else None
        if contract_type == ContractType.SPACE_LEASE:
            facts.update(
                {
                    "lease.premises": premises,
                    "lease.rent": facts["pricing.amount"],
                    "lease.commencement_date": facts["contract.start_date"],
                    "lease.expiry_date": facts["contract.end_date"],
                    "lease.rent_words": facts["pricing.amount_words"],
                }
            )
        else:
            facts.update(
                {
                    "licence.workspace": premises,
                    "licence.fee": facts["pricing.amount"],
                    "licence.commencement_date": facts["contract.start_date"],
                    "licence.expiry_date": facts["contract.end_date"],
                    "licence.fee_words": facts["pricing.amount_words"],
                }
            )
        return facts

    def _suggestions(self, contract: TenantOccupancy) -> dict[str, str]:
        tenant = self.session.get(Tenant, contract.tenant_id)
        if tenant is None:
            return {}
        out = {
            "legal_name": tenant.company_name or tenant.name,
            "company_name": tenant.company_name,
            "email": tenant.email,
            "phone": tenant.phone,
            "entity_type": "COMPANY" if tenant.company_name else None,
        }
        return {k: v for k, v in out.items() if v}

    def _documents(self, contract_id: uuid.UUID) -> list[ContractDocument]:
        return list(
            self.session.scalars(
                select(ContractDocument)
                .where(ContractDocument.contract_id == contract_id)
                .order_by(ContractDocument.created_at.desc(), ContractDocument.id)
            )
        )

    def _template(self, template_id: uuid.UUID) -> ContractTemplate:
        template = self.session.get(ContractTemplate, template_id)
        if template is None:
            raise NotFoundError("Template not found.")
        return template

    def _storage(self) -> ObjectStorage:
        if self.storage is None:
            raise ConflictError("File storage is not configured on the server.")
        return self.storage

    def _discard(self, path: str) -> None:
        # Left for purge_orphans if this fails; no row refers to it.
        with contextlib.suppress(StorageError):
            self._storage().delete(path)

    def _log(
        self,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        property_id: uuid.UUID | None,
        message: str,
    ) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            property_id=property_id,
            message=message[:500],
        )
