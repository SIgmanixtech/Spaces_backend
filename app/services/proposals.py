"""Proposals made to a lead (Phase 4A, BR-006).

- A lead with at least one proposal that is not withdrawn is a prospect. That is derived
  here, never stored as a stage, and a proposal can be made at any stage.
- DRAFT -> SENT -> ACCEPTED or REJECTED; DRAFT or SENT -> WITHDRAWN. Only a draft can be
  edited; a sent proposal is revised by making a new one (every proposal is kept).
- ACCEPTED needs the acceptance recorded (date and note). At most one accepted proposal
  per lead (also enforced by the database).
- Nothing here creates a contract, allocation, booking or tenant, or holds a space. Amounts
  and terms are stored as entered; nothing is calculated.

History goes to the lead's activity history (entity type "leads").
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.building import Building, Floor
from app.models.crm import Lead, Proposal, ProposalSpace
from app.models.enums import ProposalStatus
from app.models.property import Property
from app.models.space import Space
from app.models.user import AppUser
from app.schemas.crm import ProposalRead, ProposalSpaceRead
from app.services.base import commit, flush, log_activity
from app.services.occupancy import business_today

STATUS_LABEL = {
    ProposalStatus.DRAFT: "Draft",
    ProposalStatus.SENT: "Sent",
    ProposalStatus.ACCEPTED: "Accepted",
    ProposalStatus.REJECTED: "Rejected",
    ProposalStatus.WITHDRAWN: "Withdrawn",
}


def is_prospect_clause(lead_id: Any) -> Any:
    """SQL: the lead has at least one proposal that is not withdrawn."""
    return exists().where(
        Proposal.lead_id == lead_id, Proposal.status != ProposalStatus.WITHDRAWN.value
    )


class ProposalService:
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

    def _lead(self, lead_id: uuid.UUID) -> Lead:
        lead = self.session.get(Lead, lead_id)
        if lead is None:
            raise NotFoundError("Lead not found.")
        return lead

    def _proposal(self, proposal_id: uuid.UUID, lock: bool = False) -> Proposal:
        statement = select(Proposal).where(Proposal.id == proposal_id)
        if lock:
            statement = statement.with_for_update()
        proposal = self.session.scalar(statement)
        if proposal is None:
            raise NotFoundError("Proposal not found.")
        return proposal

    def _check_property(self, property_id: uuid.UUID) -> Property:
        prop = self.session.get(Property, property_id)
        if prop is None or prop.archived_at is not None:
            raise BadRequestError("The property does not exist or is archived.")
        return prop

    def _check_spaces(self, property_id: uuid.UUID, space_ids: list[uuid.UUID]) -> None:
        if len(set(space_ids)) != len(space_ids):
            raise BadRequestError("A space is listed twice.")
        if not space_ids:
            return
        spaces = {
            s.id: s for s in self.session.scalars(select(Space).where(Space.id.in_(space_ids)))
        }
        for space_id in space_ids:
            space = spaces.get(space_id)
            if space is None or space.property_id != property_id:
                raise BadRequestError("Every space must belong to the chosen property.")
            if space.archived_at is not None:
                raise BadRequestError(f'Space "{space.name}" is archived.')
            if not space.is_rentable:
                raise BadRequestError(f'Space "{space.name}" is not rentable.')

    def _set_spaces(self, proposal: Proposal, space_ids: list[uuid.UUID]) -> None:
        for row in self.session.scalars(
            select(ProposalSpace).where(ProposalSpace.proposal_id == proposal.id)
        ):
            self.session.delete(row)
        flush(self.session)
        for space_id in space_ids:
            self.session.add(
                ProposalSpace(
                    proposal_id=proposal.id, property_id=proposal.property_id, space_id=space_id
                )
            )

    def _require(self, proposal: Proposal, *allowed: ProposalStatus, doing: str) -> None:
        if proposal.status not in {s.value for s in allowed}:
            current = STATUS_LABEL[ProposalStatus(proposal.status)].lower()
            raise ConflictError(f"A {current} proposal cannot be {doing}.")

    def _reads(self, proposals: list[Proposal]) -> list[ProposalRead]:
        if not proposals:
            return []
        ids = [p.id for p in proposals]
        leads = dict(
            self.session.execute(
                select(Lead.id, Lead.reference).where(Lead.id.in_({p.lead_id for p in proposals}))
            ).all()
        )
        props = dict(
            self.session.execute(
                select(Property.id, Property.name).where(
                    Property.id.in_({p.property_id for p in proposals})
                )
            ).all()
        )
        users = dict(
            self.session.execute(
                select(AppUser.id, AppUser.email).where(
                    AppUser.id.in_({p.accepted_by for p in proposals if p.accepted_by})
                )
            ).all()
        )
        spaces: dict[uuid.UUID, list[ProposalSpaceRead]] = {i: [] for i in ids}
        for row, space, building, floor in self.session.execute(
            select(ProposalSpace, Space, Building.name, Floor.name)
            .join(Space, Space.id == ProposalSpace.space_id)
            .outerjoin(Building, Building.id == Space.building_id)
            .outerjoin(Floor, Floor.id == Space.floor_id)
            .where(ProposalSpace.proposal_id.in_(ids))
            .order_by(Building.name, Floor.name, Space.name, Space.id)
        ):
            spaces[row.proposal_id].append(
                ProposalSpaceRead(
                    space_id=space.id,
                    space_name=space.name,
                    space_type=space.space_type,
                    building_name=building,
                    floor_name=floor,
                    archived=space.archived_at is not None,
                )
            )
        today = business_today()
        return [
            ProposalRead(
                id=p.id,
                reference=p.reference,
                lead_id=p.lead_id,
                lead_reference=leads[p.lead_id],
                property_id=p.property_id,
                property_name=props[p.property_id],
                spaces=spaces[p.id],
                status=p.status,
                expired=p.status == ProposalStatus.SENT.value
                and p.valid_until is not None
                and p.valid_until < today,
                proposal_date=p.proposal_date,
                valid_until=p.valid_until,
                start_date=p.start_date,
                end_date=p.end_date,
                monthly_rent=float(p.monthly_rent) if p.monthly_rent is not None else None,
                security_deposit=float(p.security_deposit)
                if p.security_deposit is not None
                else None,
                currency=p.currency,
                other_charges=p.other_charges,
                commercial_terms=p.commercial_terms,
                notes=p.notes,
                sent_at=p.sent_at,
                accepted_on=p.accepted_on,
                acceptance_note=p.acceptance_note,
                accepted_at=p.accepted_at,
                accepted_by_email=users.get(p.accepted_by) if p.accepted_by else None,
                rejected_at=p.rejected_at,
                withdrawn_at=p.withdrawn_at,
                closing_reason=p.closing_reason,
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
            for p in proposals
        ]

    # --- reads ------------------------------------------------------------------------------

    def for_lead(self, lead_id: uuid.UUID) -> list[ProposalRead]:
        self._lead(lead_id)
        proposals = list(
            self.session.scalars(
                select(Proposal)
                .where(Proposal.lead_id == lead_id)
                .order_by(Proposal.created_at.desc(), Proposal.id.desc())
            )
        )
        return self._reads(proposals)

    def get(self, proposal_id: uuid.UUID) -> ProposalRead:
        return self._reads([self._proposal(proposal_id)])[0]

    # --- changes ----------------------------------------------------------------------------

    def create(self, lead_id: uuid.UUID, values: dict[str, Any]) -> ProposalRead:
        lead = self._lead(lead_id)
        space_ids = values.pop("space_ids", [])
        prop = self._check_property(values["property_id"])
        self._check_spaces(prop.id, space_ids)
        proposal = Proposal(
            lead_id=lead.id, **values, created_by=self.actor.id, updated_by=self.actor.id
        )
        self.session.add(proposal)
        flush(self.session)
        self._set_spaces(proposal, space_ids)
        flush(self.session)
        self.session.refresh(proposal)
        self._log(
            "proposal_created",
            lead,
            f'Lead {lead.reference}: proposal {proposal.reference} for "{prop.name}" created.',
        )
        commit(self.session)
        return self.get(proposal.id)

    def update(self, proposal_id: uuid.UUID, values: dict[str, Any]) -> ProposalRead:
        proposal = self._proposal(proposal_id, lock=True)
        self._require(proposal, ProposalStatus.DRAFT, doing="edited; make a new proposal")
        lead = self._lead(proposal.lead_id)
        space_ids = values.pop("space_ids", None)
        new_property = values.get("property_id", proposal.property_id)
        if new_property != proposal.property_id:
            self._check_property(new_property)
            if space_ids is None:
                space_ids = []  # The old spaces belong to the old property.
        if space_ids is not None:
            self._check_spaces(new_property, space_ids)
            # Remove the old rows before the property changes (they reference it).
            self._set_spaces(proposal, [])
            flush(self.session)
        changed = [key for key, value in values.items() if getattr(proposal, key) != value]
        for key in changed:
            setattr(proposal, key, values[key])
        if proposal.end_date is not None and proposal.end_date < proposal.start_date:
            raise BadRequestError("The end date cannot be before the start date.")
        if proposal.valid_until is not None and proposal.valid_until < proposal.proposal_date:
            raise BadRequestError("Valid until cannot be before the proposal date.")
        flush(self.session)
        if space_ids is not None:
            self._set_spaces(proposal, space_ids)
            changed.append("spaces")
        if changed:
            proposal.updated_by = self.actor.id
            self._log(
                "proposal_updated",
                lead,
                f"Lead {lead.reference}: proposal {proposal.reference} updated: "
                f"{', '.join(dict.fromkeys(changed))}.",
            )
        commit(self.session)
        return self.get(proposal.id)

    def send(self, proposal_id: uuid.UUID) -> ProposalRead:
        proposal = self._proposal(proposal_id, lock=True)
        self._require(proposal, ProposalStatus.DRAFT, doing="marked as sent")
        proposal.status = ProposalStatus.SENT.value
        proposal.sent_at = datetime.now(UTC)
        return self._close_step(proposal, "proposal_sent", "marked as sent")

    def accept(self, proposal_id: uuid.UUID, values: dict[str, Any]) -> ProposalRead:
        proposal = self._proposal(proposal_id, lock=True)
        self._require(proposal, ProposalStatus.SENT, doing="accepted; send it first")
        # Serialise acceptances for one lead, then check the one-accepted rule clearly.
        self.session.scalar(select(Lead).where(Lead.id == proposal.lead_id).with_for_update())
        other = self.session.scalar(
            select(Proposal.reference).where(
                Proposal.lead_id == proposal.lead_id,
                Proposal.status == ProposalStatus.ACCEPTED.value,
                Proposal.id != proposal.id,
            )
        )
        if other:
            raise ConflictError(
                f"Proposal {other} is already accepted for this lead. A lead has at most one "
                "accepted proposal."
            )
        proposal.status = ProposalStatus.ACCEPTED.value
        proposal.accepted_on = values["accepted_on"]
        proposal.acceptance_note = values["acceptance_note"].strip()
        if not proposal.acceptance_note:
            raise BadRequestError("Say how the acceptance was given.")
        proposal.accepted_at = datetime.now(UTC)
        proposal.accepted_by = self.actor.id
        return self._close_step(
            proposal,
            "proposal_accepted",
            f"accepted on {proposal.accepted_on.isoformat()} (recorded by an admin; no "
            "contract or allocation was created)",
        )

    def reject(self, proposal_id: uuid.UUID, reason: str | None) -> ProposalRead:
        proposal = self._proposal(proposal_id, lock=True)
        self._require(proposal, ProposalStatus.SENT, doing="rejected")
        proposal.status = ProposalStatus.REJECTED.value
        proposal.rejected_at = datetime.now(UTC)
        proposal.closing_reason = (reason or "").strip() or None
        return self._close_step(proposal, "proposal_rejected", "rejected by the lead")

    def withdraw(self, proposal_id: uuid.UUID, reason: str | None) -> ProposalRead:
        proposal = self._proposal(proposal_id, lock=True)
        self._require(proposal, ProposalStatus.DRAFT, ProposalStatus.SENT, doing="withdrawn")
        proposal.status = ProposalStatus.WITHDRAWN.value
        proposal.withdrawn_at = datetime.now(UTC)
        proposal.closing_reason = (reason or "").strip() or None
        return self._close_step(proposal, "proposal_withdrawn", "withdrawn")

    def _close_step(self, proposal: Proposal, action: str, what: str) -> ProposalRead:
        proposal.updated_by = self.actor.id
        lead = self._lead(proposal.lead_id)
        self._log(action, lead, f"Lead {lead.reference}: proposal {proposal.reference} {what}.")
        commit(self.session)
        return self.get(proposal.id)
