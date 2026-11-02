"""CRM core (Phase 3A): leads, interests, notes, follow-up tasks and duplicate warnings.

Rules (docs/BUSINESS_RULES.md):
- BR-006: stages move forward in order. Here, only the three an administrator sets:
  Lead Created -> Lead Reviewed -> Space Selected, one step at a time. The later stages
  come from contract and activation records (Phases 4-5) and are never set here.
- BR-008: every lead keeps its source; possible duplicates by phone, email or company,
  against other leads and existing tenants, are warned about and never merged, refused or
  changed. No name matching.
- No conversion to a tenant, no inventory hold for a space of interest, no reminders sent.

History goes to the existing activity log under entity type "leads" with the lead's id,
so the Activity page and a lead's history show every step.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.crm import Lead, LeadInterest, LeadNote, LeadTask
from app.models.enums import LeadSource, LeadStage, UserRole
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.repositories.base import paginate_rows, text_search
from app.schemas.common import ListQuery
from app.schemas.crm import (
    DuplicateMatch,
    InterestRead,
    LeadRead,
    NoteRead,
    OwnerRead,
    TaskRead,
)
from app.services.base import commit, flush, log_activity
from app.services.occupancy import business_today

STAGE_ORDER = [LeadStage.LEAD_CREATED, LeadStage.LEAD_REVIEWED, LeadStage.SPACE_SELECTED]
STAGE_LABEL = {
    LeadStage.LEAD_CREATED: "Lead Created",
    LeadStage.LEAD_REVIEWED: "Lead Reviewed",
    LeadStage.SPACE_SELECTED: "Space Selected",
}
SOURCE_LABEL = {
    LeadSource.MANUAL: "Manual",
    LeadSource.EXCEL: "Excel",
    LeadSource.META_ADS: "Meta Ads",
    LeadSource.OTHER: "Other",
}

# --- match keys (pure; the SQL twins below must agree) -------------------------------------


def email_key(value: str | None) -> str | None:
    return value.strip().lower() or None if value else None


def phone_key(value: str | None) -> str | None:
    """Digits only. An Indian number given as 10 digits, or 0 and 10 digits, gets 91, as
    the sign-in normaliser does, so 98765 43210, 098765 43210 and +91 98765 43210 match.
    Fewer than 8 digits is too short to compare."""
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    elif len(digits) == 10:
        digits = "91" + digits
    return digits if len(digits) >= 8 else None


def company_key(value: str | None) -> str | None:
    """Lower case, punctuation removed, spaces collapsed. No suffix stripping."""
    if not value:
        return None
    kept = "".join(ch for ch in value if ch.isalnum() or ch.isspace())
    return " ".join(kept.lower().split()) or None


def _email_sql(column: Any) -> ColumnElement[Any]:
    return func.lower(func.trim(column))


def _phone_sql(column: Any) -> ColumnElement[Any]:
    digits = func.regexp_replace(column, "[^0-9]", "", "g")
    return case(
        (
            and_(func.length(digits) == 11, func.left(digits, 1) == "0"),
            func.concat("91", func.substr(digits, 2)),
        ),
        (func.length(digits) == 10, func.concat("91", digits)),
        else_=digits,
    )


def _company_sql(column: Any) -> ColumnElement[Any]:
    stripped = func.regexp_replace(column, "[^[:alnum:][:space:]]", "", "g")
    return func.lower(func.trim(func.regexp_replace(stripped, r"\s+", " ", "g")))


def next_stage(stage: str) -> LeadStage | None:
    i = STAGE_ORDER.index(LeadStage(stage))
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


def task_state(task_done: bool, due: date, today: date) -> str:
    if task_done:
        return "DONE"
    if due < today:
        return "OVERDUE"
    return "DUE_TODAY" if due == today else "UPCOMING"


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(get_settings().business_timezone))


class CrmService:
    def __init__(self, session: Session, actor: AppUser) -> None:
        self.session = session
        self.actor = actor

    # --- helpers ----------------------------------------------------------------------------

    def _log(self, action: str, lead: Lead, message: str) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type="leads",
            entity_id=lead.id,
            property_id=lead.property_id,
            message=message,
        )

    def _lead(self, lead_id: uuid.UUID, lock: bool = False) -> Lead:
        statement = select(Lead).where(Lead.id == lead_id)
        if lock:
            statement = statement.with_for_update()
        lead = self.session.scalar(statement)
        if lead is None:
            raise NotFoundError("Lead not found.")
        return lead

    def _owner(self, owner_id: uuid.UUID | None) -> None:
        if owner_id is None:
            return
        user = self.session.get(AppUser, owner_id)
        if user is None or user.role != UserRole.ADMIN or not user.is_active:
            raise BadRequestError("The owner must be an active administrator.")

    def _property(self, property_id: uuid.UUID | None) -> None:
        if property_id is None:
            return
        prop = self.session.get(Property, property_id)
        if prop is None or prop.archived_at is not None:
            raise BadRequestError("The property does not exist or is archived.")

    @staticmethod
    def _label(lead: Lead) -> str:
        return " · ".join(p for p in (lead.company_name, lead.contact_name) if p) or lead.reference

    def _set_keys(self, lead: Lead) -> None:
        lead.email_key = email_key(lead.contact_email)
        lead.phone_key = phone_key(lead.contact_phone)
        lead.company_key = company_key(lead.company_name)

    def _reads(self, leads: list[Lead]) -> list[LeadRead]:
        if not leads:
            return []
        ids = [lead.id for lead in leads]
        owners = {
            u.id: u
            for u in self.session.scalars(
                select(AppUser).where(
                    AppUser.id.in_({lead.owner_id for lead in leads if lead.owner_id})
                )
            )
        }
        props = dict(
            self.session.execute(
                select(Property.id, Property.name).where(
                    Property.id.in_({lead.property_id for lead in leads if lead.property_id})
                )
            ).all()
        )
        today = business_today()
        counts = {
            lead_id: (open_, overdue)
            for lead_id, open_, overdue in self.session.execute(
                select(
                    LeadTask.lead_id,
                    func.count().filter(LeadTask.done.is_(False)),
                    func.count().filter(LeadTask.done.is_(False), LeadTask.due_date < today),
                )
                .where(LeadTask.lead_id.in_(ids))
                .group_by(LeadTask.lead_id)
            )
        }
        out = []
        for lead in leads:
            owner = owners.get(lead.owner_id) if lead.owner_id else None
            open_, overdue = counts.get(lead.id, (0, 0))
            out.append(
                LeadRead(
                    id=lead.id,
                    reference=lead.reference,
                    source=lead.source,
                    stage=lead.stage,
                    stage_changed_at=lead.stage_changed_at,
                    company_name=lead.company_name,
                    contact_name=lead.contact_name,
                    contact_email=lead.contact_email,
                    contact_phone=lead.contact_phone,
                    property_id=lead.property_id,
                    property_name=props.get(lead.property_id) if lead.property_id else None,
                    space_type=lead.space_type,
                    capacity=lead.capacity,
                    area_sqft=float(lead.area_sqft) if lead.area_sqft is not None else None,
                    budget_amount=float(lead.budget_amount)
                    if lead.budget_amount is not None
                    else None,
                    budget_period=lead.budget_period,
                    currency=lead.currency,
                    desired_start_date=lead.desired_start_date,
                    owner_id=lead.owner_id,
                    owner_name=owner.full_name if owner else None,
                    owner_email=owner.email if owner else None,
                    next_stage=next_stage(lead.stage),
                    open_tasks=open_,
                    overdue_tasks=overdue,
                    created_at=lead.created_at,
                    updated_at=lead.updated_at,
                )
            )
        return out

    # --- leads ------------------------------------------------------------------------------

    def leads(
        self,
        query: ListQuery,
        *,
        stage: LeadStage | None = None,
        source: str | None = None,
        owner_id: uuid.UUID | None = None,
        property_id: uuid.UUID | None = None,
        created_from: date | None = None,
        created_to: date | None = None,
    ) -> tuple[list[LeadRead], int]:
        if created_from and created_to and created_to < created_from:
            raise BadRequestError("created_to cannot be before created_from.")
        statement = select(Lead)
        for column, value in (
            (Lead.stage, stage),
            (Lead.source, source),
            (Lead.owner_id, owner_id),
            (Lead.property_id, property_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if created_from:
            statement = statement.where(Lead.created_at >= _day_start(created_from))
        if created_to:
            statement = statement.where(
                Lead.created_at < _day_start(created_to + timedelta(days=1))
            )
        if query.search:
            statement = statement.where(
                text_search(
                    [
                        Lead.reference,
                        Lead.company_name,
                        Lead.contact_name,
                        Lead.contact_email,
                        Lead.contact_phone,
                    ],
                    query.search,
                )
            )
        statement = statement.order_by(Lead.created_at.desc(), Lead.id.desc())
        rows, total = paginate_rows(self.session, statement, query)
        return self._reads([row[0] for row in rows]), total

    def get(self, lead_id: uuid.UUID) -> LeadRead:
        return self._reads([self._lead(lead_id)])[0]

    def create(self, values: dict[str, Any]) -> LeadRead:
        lead = self.insert(values)
        commit(self.session)
        return self.get(lead.id)

    def insert(
        self,
        values: dict[str, Any],
        *,
        import_batch_id: uuid.UUID | None = None,
        how: str = "",
    ) -> Lead:
        """Add one lead and its history entry without committing, so a spreadsheet import
        can add many in one transaction. `how` ends the history message, e.g. " by import"."""
        self._owner(values.get("owner_id"))
        self._property(values.get("property_id"))
        if values.get("contact_email") is not None:
            values["contact_email"] = str(values["contact_email"])
        by = self.actor.id if self.actor is not None else None  # None: the Meta sync command
        lead = Lead(
            **values,
            created_by=by,
            updated_by=by,
            import_batch_id=import_batch_id,
        )
        self._set_keys(lead)
        self.session.add(lead)
        flush(self.session)
        self.session.refresh(lead)
        self._log(
            "created", lead, f"Lead {lead.reference} ({self._label(lead)}) was created{how}."[:500]
        )
        return lead

    def update(self, lead_id: uuid.UUID, values: dict[str, Any]) -> LeadRead:
        lead = self._lead(lead_id, lock=True)
        if "owner_id" in values:
            self._owner(values["owner_id"])
        if "property_id" in values:
            self._property(values["property_id"])
        if values.get("contact_email") is not None:
            values["contact_email"] = str(values["contact_email"])
        changed = []
        old_source = lead.source
        for key, value in values.items():
            if getattr(lead, key) != value:
                changed.append(key)
                setattr(lead, key, value)
        if not (lead.company_name or lead.contact_name):
            raise BadRequestError("Give a company name or a contact name.")
        if (lead.budget_amount is None) != (lead.budget_period is None):
            raise BadRequestError("Give the budget amount together with its billing period.")
        if changed:
            self._set_keys(lead)
            lead.updated_by = self.actor.id
            self._log("updated", lead, f"Lead {lead.reference} was updated: {', '.join(changed)}.")
            if "source" in changed:
                self._log(
                    "source_changed",
                    lead,
                    f"Lead {lead.reference}: source changed from "
                    f"{SOURCE_LABEL[LeadSource(old_source)]} to "
                    f"{SOURCE_LABEL[LeadSource(lead.source)]}.",
                )
        commit(self.session)
        return self.get(lead.id)

    def change_stage(self, lead_id: uuid.UUID, stage: LeadStage) -> LeadRead:
        lead = self._lead(lead_id, lock=True)
        allowed = next_stage(lead.stage)
        if stage == lead.stage:
            raise ConflictError(f"The lead is already at {STAGE_LABEL[stage]}.")
        if stage != allowed:
            current = STAGE_LABEL[LeadStage(lead.stage)]
            raise ConflictError(
                f"A lead at {current} can only move forward to "
                f"{STAGE_LABEL[allowed] if allowed else 'the contract stages (later phases)'}."
            )
        before = STAGE_LABEL[LeadStage(lead.stage)]
        lead.stage = stage
        lead.stage_changed_at = datetime.now(UTC)
        lead.updated_by = self.actor.id
        self._log(
            "stage_changed",
            lead,
            f"Lead {lead.reference} moved from {before} to {STAGE_LABEL[stage]}.",
        )
        commit(self.session)
        return self.get(lead.id)

    def owners(self) -> list[OwnerRead]:
        users = self.session.scalars(
            select(AppUser)
            .where(AppUser.role == UserRole.ADMIN, AppUser.is_active.is_(True))
            .order_by(AppUser.email)
        )
        return [OwnerRead(id=u.id, name=u.full_name, email=u.email) for u in users]

    # --- interests ----------------------------------------------------------------------------

    def interests(self, lead_id: uuid.UUID) -> list[InterestRead]:
        self._lead(lead_id)
        rows = self.session.execute(
            select(LeadInterest, Property.name, Space.name, Space.archived_at)
            .join(Property, Property.id == LeadInterest.property_id)
            .outerjoin(Space, Space.id == LeadInterest.space_id)
            .where(LeadInterest.lead_id == lead_id)
            .order_by(LeadInterest.created_at, LeadInterest.id)
        ).all()
        return [
            InterestRead(
                id=i.id,
                lead_id=i.lead_id,
                property_id=i.property_id,
                property_name=pname,
                space_id=i.space_id,
                space_name=sname,
                space_archived=archived is not None,
                note=i.note,
                created_at=i.created_at,
            )
            for i, pname, sname, archived in rows
        ]

    def add_interest(self, lead_id: uuid.UUID, values: dict[str, Any]) -> InterestRead:
        lead = self._lead(lead_id)
        self._property(values["property_id"])
        prop = self.session.get(Property, values["property_id"])
        where = f'"{prop.name}"'
        if values.get("space_id") is not None:
            space = self.session.get(Space, values["space_id"])
            if space is None or space.property_id != values["property_id"]:
                raise BadRequestError("The space must belong to the chosen property.")
            if space.archived_at is not None:
                raise BadRequestError("The space is archived.")
            where = f'space "{space.name}" at "{prop.name}"'
        interest = LeadInterest(lead_id=lead.id, **values, created_by=self.actor.id)
        self.session.add(interest)
        flush(self.session)
        self._log("interest_added", lead, f"Lead {lead.reference}: interest in {where} added.")
        commit(self.session)
        return next(i for i in self.interests(lead.id) if i.id == interest.id)

    def remove_interest(self, lead_id: uuid.UUID, interest_id: uuid.UUID) -> None:
        lead = self._lead(lead_id)
        interest = self.session.get(LeadInterest, interest_id)
        if interest is None or interest.lead_id != lead.id:
            raise NotFoundError("Interest not found.")
        self._log("interest_removed", lead, f"Lead {lead.reference}: an interest was removed.")
        self.session.delete(interest)
        commit(self.session)

    # --- notes -------------------------------------------------------------------------------

    def notes(self, lead_id: uuid.UUID) -> list[NoteRead]:
        self._lead(lead_id)
        rows = self.session.execute(
            select(LeadNote, AppUser.email)
            .outerjoin(AppUser, AppUser.id == LeadNote.created_by)
            .where(LeadNote.lead_id == lead_id)
            .order_by(LeadNote.created_at.desc(), LeadNote.id.desc())
        ).all()
        return [
            NoteRead(
                id=n.id,
                lead_id=n.lead_id,
                body=n.body,
                author_email=email,
                created_at=n.created_at,
                updated_at=n.updated_at,
            )
            for n, email in rows
        ]

    def _note(self, lead: Lead, note_id: uuid.UUID) -> LeadNote:
        note = self.session.get(LeadNote, note_id)
        if note is None or note.lead_id != lead.id:
            raise NotFoundError("Note not found.")
        return note

    def add_note(self, lead_id: uuid.UUID, body: str) -> NoteRead:
        lead = self._lead(lead_id)
        note = LeadNote(
            lead_id=lead.id, body=body, created_by=self.actor.id, updated_by=self.actor.id
        )
        self.session.add(note)
        flush(self.session)
        self._log("note_added", lead, f"Lead {lead.reference}: a note was added.")
        commit(self.session)
        return next(n for n in self.notes(lead.id) if n.id == note.id)

    def update_note(self, lead_id: uuid.UUID, note_id: uuid.UUID, body: str) -> NoteRead:
        lead = self._lead(lead_id)
        note = self._note(lead, note_id)
        if note.body != body:
            note.body = body
            note.updated_by = self.actor.id
            self._log("note_updated", lead, f"Lead {lead.reference}: a note was edited.")
        commit(self.session)
        return next(n for n in self.notes(lead.id) if n.id == note.id)

    def delete_note(self, lead_id: uuid.UUID, note_id: uuid.UUID) -> None:
        lead = self._lead(lead_id)
        note = self._note(lead, note_id)
        self._log("note_deleted", lead, f"Lead {lead.reference}: a note was deleted.")
        self.session.delete(note)
        commit(self.session)

    # --- tasks -------------------------------------------------------------------------------

    def _task_reads(self, rows: list[tuple[LeadTask, Lead]]) -> list[TaskRead]:
        today = business_today()
        owners = (
            {
                u.id: u
                for u in self.session.scalars(
                    select(AppUser).where(
                        AppUser.id.in_({t.owner_id for t, _ in rows if t.owner_id})
                    )
                )
            }
            if rows
            else {}
        )
        return [
            TaskRead(
                id=t.id,
                lead_id=t.lead_id,
                lead_reference=lead.reference,
                lead_label=self._label(lead),
                title=t.title,
                details=t.details,
                due_date=t.due_date,
                state=task_state(t.done, t.due_date, today),
                owner_id=t.owner_id,
                owner_name=(owners[t.owner_id].full_name or owners[t.owner_id].email)
                if t.owner_id in owners
                else None,
                done=t.done,
                completed_at=t.completed_at,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
            for t, lead in rows
        ]

    def tasks(
        self,
        query: ListQuery,
        *,
        lead_id: uuid.UUID | None = None,
        due: str = "open",
        owner_id: uuid.UUID | None = None,
    ) -> tuple[list[TaskRead], int]:
        """due: overdue, today, upcoming, open (not done), done or all."""
        if lead_id is not None:
            self._lead(lead_id)
        today = business_today()
        filters: dict[str, ColumnElement[bool]] = {
            "overdue": and_(LeadTask.done.is_(False), LeadTask.due_date < today),
            "today": and_(LeadTask.done.is_(False), LeadTask.due_date == today),
            "upcoming": and_(LeadTask.done.is_(False), LeadTask.due_date > today),
            "open": LeadTask.done.is_(False),
            "done": LeadTask.done.is_(True),
        }
        if due not in filters and due != "all":
            raise BadRequestError("due must be one of: overdue, today, upcoming, open, done, all.")
        statement = select(LeadTask, Lead).join(Lead, Lead.id == LeadTask.lead_id)
        if due != "all":
            statement = statement.where(filters[due])
        if lead_id is not None:
            statement = statement.where(LeadTask.lead_id == lead_id)
        if owner_id is not None:
            statement = statement.where(LeadTask.owner_id == owner_id)
        if query.search:
            statement = statement.where(
                text_search(
                    [LeadTask.title, Lead.reference, Lead.company_name, Lead.contact_name],
                    query.search,
                )
            )
        statement = statement.order_by(
            LeadTask.done, LeadTask.due_date, LeadTask.created_at, LeadTask.id
        )
        rows, total = paginate_rows(self.session, statement, query)
        return self._task_reads([(t, lead) for t, lead in rows]), total

    def _task(self, lead: Lead, task_id: uuid.UUID) -> LeadTask:
        task = self.session.scalar(select(LeadTask).where(LeadTask.id == task_id).with_for_update())
        if task is None or task.lead_id != lead.id:
            raise NotFoundError("Task not found.")
        return task

    def _one_task(self, task_id: uuid.UUID) -> TaskRead:
        row = self.session.execute(
            select(LeadTask, Lead)
            .join(Lead, Lead.id == LeadTask.lead_id)
            .where(LeadTask.id == task_id)
        ).one()
        return self._task_reads([(row[0], row[1])])[0]

    def add_task(self, lead_id: uuid.UUID, values: dict[str, Any]) -> TaskRead:
        lead = self._lead(lead_id)
        self._owner(values.get("owner_id"))
        task = LeadTask(
            lead_id=lead.id, **values, created_by=self.actor.id, updated_by=self.actor.id
        )
        self.session.add(task)
        flush(self.session)
        self._log(
            "task_added",
            lead,
            f'Lead {lead.reference}: follow-up "{task.title}" '
            f"due {task.due_date.isoformat()} added.",
        )
        commit(self.session)
        return self._one_task(task.id)

    def update_task(
        self, lead_id: uuid.UUID, task_id: uuid.UUID, values: dict[str, Any]
    ) -> TaskRead:
        lead = self._lead(lead_id)
        task = self._task(lead, task_id)
        if "owner_id" in values:
            self._owner(values["owner_id"])
        done = values.pop("done", None)
        for key, value in values.items():
            setattr(task, key, value)
        if done is not None and done != task.done:
            task.done = done
            task.completed_at = datetime.now(UTC) if done else None
            task.completed_by = self.actor.id if done else None
            verb = "completed" if done else "reopened"
            self._log(
                f"task_{verb}", lead, f'Lead {lead.reference}: follow-up "{task.title}" {verb}.'
            )
        elif values:
            self._log(
                "task_updated", lead, f'Lead {lead.reference}: follow-up "{task.title}" updated.'
            )
        task.updated_by = self.actor.id
        commit(self.session)
        return self._one_task(task.id)

    def delete_task(self, lead_id: uuid.UUID, task_id: uuid.UUID) -> None:
        lead = self._lead(lead_id)
        task = self._task(lead, task_id)
        self._log("task_deleted", lead, f'Lead {lead.reference}: follow-up "{task.title}" deleted.')
        self.session.delete(task)
        commit(self.session)

    # --- duplicates -------------------------------------------------------------------------

    def check_duplicates(
        self,
        *,
        contact_email: str | None,
        contact_phone: str | None,
        company_name: str | None,
        exclude_lead_id: uuid.UUID | None = None,
        exclude_tenant_id: uuid.UUID | None = None,
        include_leads: bool = True,
    ) -> list[DuplicateMatch]:
        """Possible duplicates among leads and tenants. Read-only; nothing is changed.
        The tenant form passes include_leads=False and the tenant being edited."""
        keys = {
            "email": email_key(contact_email),
            "phone": phone_key(contact_phone),
            "company": company_key(company_name),
        }
        return self.match_many(
            [keys],
            exclude_lead_id=exclude_lead_id,
            exclude_tenant_id=exclude_tenant_id,
            include_leads=include_leads,
        )[0]

    def match_many(
        self,
        key_sets: list[dict[str, str | None]],
        *,
        exclude_lead_id: uuid.UUID | None = None,
        exclude_tenant_id: uuid.UUID | None = None,
        include_leads: bool = True,
        limit: int = 50,
    ) -> list[list[DuplicateMatch]]:
        """Possible duplicates for many sets of normalised keys (email, phone, company) in
        two queries, one answer per set in the same order. The same matching as the lead
        and tenant forms: never by name alone. Read-only."""
        wanted = {k: {ks[k] for ks in key_sets if ks.get(k)} for k in ("email", "phone", "company")}
        answers: list[list[DuplicateMatch]] = [[] for _ in key_sets]
        if not any(wanted.values()):
            return answers
        lead_cols = {"email": Lead.email_key, "phone": Lead.phone_key, "company": Lead.company_key}
        tenant_cols = {
            "email": _email_sql(Tenant.email),
            "phone": _phone_sql(Tenant.phone),
            "company": _company_sql(Tenant.company_name),
        }

        def matching(cols: dict[str, ColumnElement[Any]]) -> list[ColumnElement[bool]]:
            return [cols[k].in_(sorted(v)) for k, v in wanted.items() if v]

        def matched_on(keys: dict[str, str | None], found: dict[str, str | None]) -> list[str]:
            return [k for k in ("email", "phone", "company") if keys.get(k) and found[k] == keys[k]]

        if include_leads:
            lead_q = select(Lead).where(or_(*matching(lead_cols)))
            if exclude_lead_id is not None:
                lead_q = lead_q.where(Lead.id != exclude_lead_id)
            for lead in self.session.scalars(lead_q.order_by(Lead.created_at.desc(), Lead.id)):
                found = {
                    "email": lead.email_key,
                    "phone": lead.phone_key,
                    "company": lead.company_key,
                }
                for keys, out in zip(key_sets, answers, strict=True):
                    on = matched_on(keys, found)
                    if on and sum(m.kind == "LEAD" for m in out) < limit:
                        out.append(
                            DuplicateMatch(
                                kind="LEAD",
                                id=lead.id,
                                reference=lead.reference,
                                name=self._label(lead),
                                company_name=lead.company_name,
                                email=lead.contact_email,
                                phone=lead.contact_phone,
                                status=STAGE_LABEL[LeadStage(lead.stage)],
                                archived=False,
                                matched_on=on,  # type: ignore[arg-type]
                            )
                        )
        tenant_q = select(
            Tenant,
            tenant_cols["email"].label("e"),
            tenant_cols["phone"].label("p"),
            tenant_cols["company"].label("c"),
        ).where(or_(*matching(tenant_cols)))
        if exclude_tenant_id is not None:
            tenant_q = tenant_q.where(Tenant.id != exclude_tenant_id)
        for tenant, e, p, c in self.session.execute(tenant_q.order_by(Tenant.name, Tenant.id)):
            found = {"email": e, "phone": p, "company": c}
            for keys, out in zip(key_sets, answers, strict=True):
                on = matched_on(keys, found)
                if on and sum(m.kind == "TENANT" for m in out) < limit:
                    out.append(
                        DuplicateMatch(
                            kind="TENANT",
                            id=tenant.id,
                            reference=None,
                            name=tenant.name,
                            company_name=tenant.company_name,
                            email=tenant.email,
                            phone=tenant.phone,
                            status=tenant.status,
                            archived=tenant.archived_at is not None,
                            matched_on=on,  # type: ignore[arg-type]
                        )
                    )
        return answers

    def duplicates_of(self, lead_id: uuid.UUID) -> list[DuplicateMatch]:
        lead = self._lead(lead_id)
        return self.check_duplicates(
            contact_email=lead.contact_email,
            contact_phone=lead.contact_phone,
            company_name=lead.company_name,
            exclude_lead_id=lead.id,
        )
