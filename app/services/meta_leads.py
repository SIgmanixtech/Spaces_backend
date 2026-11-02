"""Meta Ads lead intake (Phase 3B-2, polling; BR-006, BR-008).

Approved rules (2 Nov 2026): leads arrive by polling (a scheduled `sync-meta-leads`
command and a "Sync now" button); they follow the Excel import's rules: possible
duplicates are imported with a warning and never merged, the owner is left unassigned,
answers that fill no lead field become the lead's first note, and each form's field
mapping is set by an admin. Imported leads have source META_ADS and start at Lead Created.

Every lead fetched from Meta is stored once (unique Meta lead id) before it is processed,
so fetching it again, or a retried sync, never creates a second CRM lead. Each lead is
processed in its own transaction: one that cannot become a lead is marked FAILED with the
reason and can be reprocessed after its form's mapping is corrected. Nothing is retried
automatically.

The Meta client is swappable: tests use a stand-in, and the Graph API client is used only
when the server is configured. The page token and app secret are never logged, stored in
the database or returned by the API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.models.crm import Lead, LeadNote
from app.models.enums import LeadSource, MetaLeadStatus
from app.models.meta import MetaLeadEvent, MetaLeadForm
from app.models.user import AppUser
from app.repositories.base import paginate_rows
from app.schemas.common import ListQuery
from app.schemas.lead_import import ImportField
from app.schemas.meta_lead import (
    MetaAnswer,
    MetaDuplicate,
    MetaEventRead,
    MetaFieldOptions,
    MetaFormRead,
    MetaStatus,
    MetaSyncFormResult,
    MetaSyncResult,
)
from app.services.base import commit, flush, log_activity
from app.services.crm import CrmService
from app.services.lead_import_files import FIELD_LABEL, FIELDS, Cell, check_row
from app.services.lead_imports import LeadImportService

log = logging.getLogger(__name__)

# A Meta question can fill any import field except the owner: Meta leads stay unassigned.
MAPPABLE = [key for key, _ in FIELDS if key != "owner_id"]
# Meta's standard lead-form questions, offered as a starting mapping the admin can change.
SUGGESTED = {
    "full_name": "contact_name",
    "email": "contact_email",
    "phone_number": "contact_phone",
    "company_name": "company_name",
}
MAX_NOTE = 5000


class MetaClientError(Exception):
    """Meta could not be reached or refused the request. The message never holds secrets."""


class MetaClient(Protocol):
    def fetch_leads(self, form_id: str, since: datetime | None) -> list[dict[str, Any]]:
        """Leads of one form created after `since` (all of them when None)."""
        ...


class GraphMetaClient:
    """Meta's Graph API. Not exercised against Meta in development: the request shape is
    to be confirmed with live credentials (D14) before the integration is relied on."""

    FIELDS = "id,created_time,field_data,ad_id,adset_id,campaign_id,form_id"

    def __init__(self, settings: Settings) -> None:
        self.version = settings.meta_graph_api_version or ""
        self._token = settings.meta_page_access_token.get_secret_value()  # type: ignore[union-attr]
        self._secret = settings.meta_app_secret.get_secret_value()  # type: ignore[union-attr]
        self.timeout = settings.meta_timeout_seconds

    def _proof(self) -> str:
        return hmac.new(self._secret.encode(), self._token.encode(), hashlib.sha256).hexdigest()

    def fetch_leads(self, form_id: str, since: datetime | None) -> list[dict[str, Any]]:
        params: dict[str, Any] | None = {
            "access_token": self._token,
            "appsecret_proof": self._proof(),
            "fields": self.FIELDS,
            "limit": 100,
        }
        if since is not None:
            params["filtering"] = json.dumps(
                [
                    {
                        "field": "time_created",
                        "operator": "GREATER_THAN",
                        "value": int(since.timestamp()),
                    }
                ]
            )
        url: str | None = f"https://graph.facebook.com/{self.version}/{form_id}/leads"
        leads: list[dict[str, Any]] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                while url and len(leads) < 10_000:
                    response = client.get(url, params=params)
                    if response.status_code >= 400:
                        raise MetaClientError(_meta_error(response))
                    body = response.json()
                    leads.extend(body.get("data") or [])
                    url = (body.get("paging") or {}).get("next")
                    # The "next" link carries every parameter; passing any (even an
                    # empty set) would replace its query and fetch page one again.
                    params = None
        except httpx.HTTPError as exc:
            raise MetaClientError(f"Meta could not be reached ({type(exc).__name__}).") from exc
        return leads


def _meta_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    code = error.get("code")
    kind = error.get("type")
    # Meta's message may quote the request; keep only its code and type.
    return f"Meta refused the request (HTTP {response.status_code}" + (
        f", {kind} code {code})." if code or kind else ")."
    )


def missing_settings(settings: Settings) -> list[str]:
    names = {
        "META_GRAPH_API_VERSION": settings.meta_graph_api_version,
        "META_PAGE_ACCESS_TOKEN": settings.meta_page_access_token,
        "META_APP_SECRET": settings.meta_app_secret,
    }
    return [name for name, value in names.items() if not value]


def client_from_settings(settings: Settings) -> MetaClient | None:
    return None if missing_settings(settings) else GraphMetaClient(settings)


# --- reading Meta leads ------------------------------------------------------------------


def answers_of(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(question name, answer) pairs; several values of one question are joined."""
    out = []
    for item in payload.get("field_data") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        values = [str(v).strip() for v in item.get("values") or [] if str(v).strip()]
        out.append((str(item["name"]), ", ".join(values)))
    return out


def meta_time(value: object) -> datetime | None:
    """Meta writes times like 2026-10-31T10:00:00+0000."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def check_mapping(mapping: dict[str, str | None]) -> dict[str, str]:
    clean: dict[str, str] = {}
    used: set[str] = set()
    for question, key in mapping.items():
        name = question.strip()
        if not name or len(name) > 200:
            raise BadRequestError("Each Meta question name must be 1 to 200 characters.")
        if key is None:
            continue
        if key not in MAPPABLE:
            raise BadRequestError(
                "Meta leads are left unassigned: the owner cannot be mapped."
                if key == "owner_id"
                else f"Unknown lead field: {key}."
            )
        if key in used:
            raise BadRequestError(
                f"Two questions are mapped to {FIELD_LABEL[key]}. Map each field once."
            )
        used.add(key)
        clean[name] = key
    return clean


def build_note(mapped_note: str | None, unmapped: list[tuple[str, str]]) -> str | None:
    """The first note: the mapped Notes answer, then every answer no lead field took."""
    parts = [mapped_note] if mapped_note else []
    parts += [f"{name}: {value}" for name, value in unmapped if value]
    if not parts:
        return None
    body = "Answers from the Meta lead form:\n" + "\n".join(parts) if unmapped else parts[0]
    if len(body) > MAX_NOTE:
        # Every answer stays in the stored Meta lead; the note shows as much as it can hold.
        body = body[: MAX_NOTE - 1] + "…"
    return body


# --- the service -------------------------------------------------------------------------


class MetaLeadService:
    def __init__(self, session: Session, actor: AppUser | None, client: MetaClient | None) -> None:
        self.session = session
        self.actor = actor
        self.client = client
        self.crm = CrmService(session, actor)  # type: ignore[arg-type]

    # forms -----------------------------------------------------------------------------

    def field_options(self) -> MetaFieldOptions:
        return MetaFieldOptions(
            fields=[ImportField(key=k, label=FIELD_LABEL[k]) for k in MAPPABLE],
            suggested=SUGGESTED,
        )

    def _form(self, form_id: uuid.UUID) -> MetaLeadForm:
        form = self.session.get(MetaLeadForm, form_id)
        if form is None:
            raise NotFoundError("Meta form not found.")
        return form

    def _counts(self, form_ref: uuid.UUID | None = None) -> dict[str, int]:
        statement = select(MetaLeadEvent.status, func.count()).group_by(MetaLeadEvent.status)
        if form_ref is not None:
            statement = statement.where(MetaLeadEvent.form_ref == form_ref)
        found = dict(self.session.execute(statement).all())
        return {s.value.lower(): int(found.get(s.value, 0)) for s in MetaLeadStatus}

    def _form_read(self, form: MetaLeadForm) -> MetaFormRead:
        seen: set[str] = set()
        for (payload,) in self.session.execute(
            select(MetaLeadEvent.payload)
            .where(MetaLeadEvent.form_ref == form.id)
            .order_by(MetaLeadEvent.created_at.desc())
            .limit(200)
        ):
            seen.update(name for name, _ in answers_of(payload))
        counts = self._counts(form.id)
        return MetaFormRead(
            id=form.id,
            form_id=form.form_id,
            name=form.name,
            enabled=form.enabled,
            field_mapping=dict(form.field_mapping),
            seen_fields=sorted(seen),
            last_synced_at=form.last_synced_at,
            last_lead_time=form.last_lead_time,
            last_sync_error=form.last_sync_error,
            received=counts["received"],
            imported=counts["imported"],
            failed=counts["failed"],
            created_at=form.created_at,
            updated_at=form.updated_at,
        )

    def forms(self) -> list[MetaFormRead]:
        rows = self.session.scalars(
            select(MetaLeadForm).order_by(MetaLeadForm.name, MetaLeadForm.id)
        )
        return [self._form_read(f) for f in rows]

    def add_form(self, form_id: str, name: str) -> MetaFormRead:
        if self.session.scalar(select(MetaLeadForm).where(MetaLeadForm.form_id == form_id)):
            raise ConflictError("This Meta form is already added.")
        form = MetaLeadForm(
            form_id=form_id,
            name=name,
            enabled=False,
            field_mapping=dict(SUGGESTED),
            created_by=self.actor.id if self.actor else None,
            updated_by=self.actor.id if self.actor else None,
        )
        self.session.add(form)
        flush(self.session)
        self._log("created", form, f"Meta lead form {name} ({form_id}) was added, not enabled.")
        commit(self.session)
        return self._form_read(form)

    def update_form(self, form_ref: uuid.UUID, values: dict[str, Any]) -> MetaFormRead:
        form = self._form(form_ref)
        changed = []
        if "field_mapping" in values:
            mapping = check_mapping(values["field_mapping"])
            if mapping != form.field_mapping:
                form.field_mapping = mapping
                changed.append("field mapping")
        for key in ("name", "enabled"):
            if key in values and getattr(form, key) != values[key]:
                setattr(form, key, values[key])
                changed.append(key)
        if changed:
            form.updated_by = self.actor.id if self.actor else None
            state = "enabled" if form.enabled else "not enabled"
            self._log(
                "updated",
                form,
                f"Meta lead form {form.name}: {', '.join(changed)} changed ({state}).",
            )
            commit(self.session)
        return self._form_read(form)

    def status(self, settings: Settings) -> MetaStatus:
        counts = self._counts()
        return MetaStatus(
            configured=not missing_settings(settings),
            missing_settings=missing_settings(settings),
            forms=self.session.scalar(select(func.count()).select_from(MetaLeadForm)) or 0,
            enabled_forms=self.session.scalar(
                select(func.count()).select_from(MetaLeadForm).where(MetaLeadForm.enabled.is_(True))
            )
            or 0,
            last_synced_at=self.session.scalar(select(func.max(MetaLeadForm.last_synced_at))),
            received=counts["received"],
            imported=counts["imported"],
            failed=counts["failed"],
        )

    # events ----------------------------------------------------------------------------

    def _event_read(
        self, event: MetaLeadEvent, form: MetaLeadForm, reference: str | None
    ) -> MetaEventRead:
        mapping = form.field_mapping
        return MetaEventRead(
            id=event.id,
            leadgen_id=event.leadgen_id,
            form_ref=event.form_ref,
            form_name=form.name,
            ad_id=event.ad_id,
            adset_id=event.adset_id,
            campaign_id=event.campaign_id,
            meta_created_time=event.meta_created_time,
            status=MetaLeadStatus(event.status),
            attempts=event.attempts,
            last_error=event.last_error,
            duplicates=[MetaDuplicate.model_validate(d) for d in event.duplicates],
            lead_id=event.lead_id,
            lead_reference=reference,
            answers=[
                MetaAnswer(name=n, value=v, field=mapping.get(n))
                for n, v in answers_of(event.payload)
            ],
            processed_at=event.processed_at,
            created_at=event.created_at,
        )

    def events(
        self,
        query: ListQuery,
        status: MetaLeadStatus | None = None,
        form_ref: uuid.UUID | None = None,
    ) -> tuple[list[MetaEventRead], int]:
        statement = (
            select(MetaLeadEvent, MetaLeadForm, Lead.reference)
            .join(MetaLeadForm, MetaLeadForm.id == MetaLeadEvent.form_ref)
            .outerjoin(Lead, Lead.id == MetaLeadEvent.lead_id)
            .order_by(MetaLeadEvent.created_at.desc(), MetaLeadEvent.id)
        )
        if status is not None:
            statement = statement.where(MetaLeadEvent.status == status)
        if form_ref is not None:
            statement = statement.where(MetaLeadEvent.form_ref == form_ref)
        if query.search:
            statement = statement.where(MetaLeadEvent.leadgen_id.contains(query.search.strip()))
        rows, total = paginate_rows(self.session, statement, query)
        return [self._event_read(e, f, r) for e, f, r in rows], total

    def event(self, event_id: uuid.UUID) -> MetaEventRead:
        found = self.session.execute(
            select(MetaLeadEvent, MetaLeadForm, Lead.reference)
            .join(MetaLeadForm, MetaLeadForm.id == MetaLeadEvent.form_ref)
            .outerjoin(Lead, Lead.id == MetaLeadEvent.lead_id)
            .where(MetaLeadEvent.id == event_id)
        ).first()
        if found is None:
            raise NotFoundError("Meta lead not found.")
        return self._event_read(*found)

    # intake ----------------------------------------------------------------------------

    def receive(self, form: MetaLeadForm, payload: dict[str, Any]) -> MetaLeadEvent | None:
        """Store one Meta lead unless it is already stored. Returns the new event, if any."""
        leadgen_id = str(payload.get("id") or "").strip()
        if not leadgen_id or len(leadgen_id) > 64:
            raise MetaClientError("Meta returned a lead without a valid id.")
        if self.session.scalar(
            select(MetaLeadEvent.id).where(MetaLeadEvent.leadgen_id == leadgen_id)
        ):
            return None
        event = MetaLeadEvent(
            leadgen_id=leadgen_id,
            form_ref=form.id,
            ad_id=_short(payload.get("ad_id")),
            adset_id=_short(payload.get("adset_id")),
            campaign_id=_short(payload.get("campaign_id")),
            meta_created_time=meta_time(payload.get("created_time")),
            payload=payload,
            status=MetaLeadStatus.RECEIVED,
            attempts=0,
        )
        self.session.add(event)
        flush(self.session)
        commit(self.session)
        return event

    def process(self, event: MetaLeadEvent) -> MetaLeadEvent:
        """Turn one stored Meta lead into a CRM lead (once), or record why it cannot."""
        if event.status == MetaLeadStatus.IMPORTED:
            raise ConflictError("This Meta lead is already imported.")
        form = self.session.get(MetaLeadForm, event.form_ref)
        assert form is not None
        event_id = event.id
        try:
            answers = answers_of(event.payload)
            headers = [name for name, _ in answers]
            mapping = [form.field_mapping.get(name) for name in headers]
            lookups = LeadImportService(self.session, self.actor)._lookups()  # type: ignore[arg-type]
            check = check_row(
                1,
                [Cell(value=value) for _, value in answers],
                headers,
                mapping,
                lookups,
                source=LeadSource.META_ADS,
            )
            event.attempts += 1
            event.processed_at = datetime.now(UTC)
            if check.errors or check.lead is None:
                event.status = MetaLeadStatus.FAILED
                event.last_error = "; ".join(
                    f"{e['column']}: {e['message']}" for e in check.errors
                )[:2000] or ("The answers do not make a lead.")
                commit(self.session)
                return event
            values = dict(check.lead)
            values["owner_id"] = None  # approved: Meta leads are left unassigned
            dupes = self.crm.match_many([check.keys])[0]
            lead = self.crm.insert(
                values, how=f" from Meta Ads form {form.name} (Meta lead {event.leadgen_id})"
            )
            unmapped = [(n, v) for n, v in answers if not form.field_mapping.get(n)]
            note = build_note(check.note, unmapped)
            if note:
                self.session.add(
                    LeadNote(
                        lead_id=lead.id,
                        body=note,
                        created_by=self.actor.id if self.actor else None,
                        updated_by=self.actor.id if self.actor else None,
                    )
                )
                self.crm._log("note_added", lead, f"Lead {lead.reference}: a note was added.")
            event.status = MetaLeadStatus.IMPORTED
            event.lead_id = lead.id
            event.last_error = None
            event.duplicates = [
                {
                    "kind": d.kind,
                    "id": str(d.id),
                    "reference": d.reference,
                    "name": d.name,
                    "matched_on": list(d.matched_on),
                }
                for d in dupes
            ]
            commit(self.session)
            return event
        except Exception:
            # Nothing of this lead is kept; the stored Meta lead is marked failed instead.
            self.session.rollback()
            log.exception("Meta lead %s could not be processed", event_id)
            again = self.session.get(MetaLeadEvent, event_id)
            assert again is not None
            again.attempts += 1
            again.status = MetaLeadStatus.FAILED
            again.last_error = (
                "An unexpected error stopped this lead from being imported. Try again."
            )
            again.processed_at = datetime.now(UTC)
            commit(self.session)
            return again

    def reprocess(self, event_id: uuid.UUID) -> MetaEventRead:
        event = self.session.get(MetaLeadEvent, event_id)
        if event is None:
            raise NotFoundError("Meta lead not found.")
        if event.status != MetaLeadStatus.FAILED:
            raise ConflictError(
                "Only a failed Meta lead can be processed again."
                if event.status != MetaLeadStatus.IMPORTED
                else "This Meta lead is already imported."
            )
        self.process(event)
        return self.event(event_id)

    def sync(self, form_refs: list[uuid.UUID] | None = None) -> MetaSyncResult:
        """Fetch new leads of the enabled forms (or the given ones) and import them."""
        if self.client is None:
            raise ServiceUnavailableError("Meta Ads is not configured on the server.")
        statement = (
            select(MetaLeadForm).where(MetaLeadForm.enabled.is_(True)).order_by(MetaLeadForm.name)
        )
        if form_refs:
            statement = statement.where(MetaLeadForm.id.in_(form_refs))
        results: list[MetaSyncFormResult] = []
        for form in list(self.session.scalars(statement)):
            result = MetaSyncFormResult(
                form_id=form.form_id,
                name=form.name,
                received=0,
                already_known=0,
                imported=0,
                failed=0,
                error=None,
            )
            try:
                leads = self.client.fetch_leads(form.form_id, form.last_lead_time)
                newest = form.last_lead_time
                for payload in leads:
                    created = meta_time(payload.get("created_time"))
                    if created and (newest is None or created > newest):
                        newest = created
                    event = self.receive(form, payload)
                    if event is None:
                        result.already_known += 1
                        continue
                    result.received += 1
                    done = self.process(event)
                    if done.status == MetaLeadStatus.IMPORTED:
                        result.imported += 1
                    else:
                        result.failed += 1
                form = self.session.get(MetaLeadForm, form.id)  # type: ignore[assignment]
                form.last_lead_time = newest
                form.last_sync_error = None
            except MetaClientError as exc:
                self.session.rollback()
                form = self.session.get(MetaLeadForm, form.id)  # type: ignore[assignment]
                result.error = form.last_sync_error = str(exc)[:500]
            form.last_synced_at = datetime.now(UTC)
            if result.received or result.error:
                self._log(
                    "synced",
                    form,
                    f"Meta Ads sync of {form.name}: {result.received} new, "
                    f"{result.imported} imported, {result.failed} failed"
                    + (f"; {result.error}" if result.error else "")
                    + ".",
                )
            commit(self.session)
            results.append(result)
        return MetaSyncResult(
            forms=results,
            received=sum(r.received for r in results),
            imported=sum(r.imported for r in results),
            failed=sum(r.failed for r in results),
        )

    def _log(self, action: str, form: MetaLeadForm, message: str) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type="meta_lead_forms",
            entity_id=form.id,
            property_id=None,
            message=message[:500],
        )


def _short(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:64] or None
