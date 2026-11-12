"""Lead stages after Space Selected (owner decisions of 2 Nov 2026, BR-006).

L-2: Contract Prepared -> Contract Completed -> Tenant Activated move automatically from
the records, never by hand:
- Contract Prepared: a contract made from one of the lead's proposals exists and is not
  cancelled.
- Contract Completed (L-4): that contract is completed (BR-025: approved and signed).
  A contract later terminated was completed, so the lead stays there (history is kept).
- Tenant Activated: and its tenant has activated client-app access.
Nothing is stored on the lead: the stage is read from the contract, agreement and tenant
records, so it always matches them. Each change is written to the lead's history once,
with what caused it.

L-4: the main contract drives the stage: a lead's Parking contracts count only when it has
no other contract (L-5: a parking-only lead follows its Parking contract). Contracts
without a category (made before categories) count as main contracts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.activity import ActivityLog
from app.models.agreement import ContractDocument
from app.models.crm import Lead, Proposal
from app.models.enums import ContractCategory, ContractStatus, DocumentStatus, LeadStage
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.services.base import log_activity

CONTRACT_PREPARED = "CONTRACT_PREPARED"
CONTRACT_COMPLETED = "CONTRACT_COMPLETED"
TENANT_ACTIVATED = "TENANT_ACTIVATED"
PIPELINE = [s.value for s in LeadStage] + [CONTRACT_PREPARED, CONTRACT_COMPLETED, TENANT_ACTIVATED]
LABEL = {
    LeadStage.LEAD_CREATED.value: "Lead Created",
    LeadStage.LEAD_REVIEWED.value: "Lead Reviewed",
    LeadStage.SPACE_SELECTED.value: "Space Selected",
    CONTRACT_PREPARED: "Contract Prepared",
    CONTRACT_COMPLETED: "Contract Completed",
    TENANT_ACTIVATED: "Tenant Activated",
}
ACTION = "stage_auto"  # activity action; the message names the stage


def pipeline_stages(session: Session, leads: Iterable[Lead]) -> dict[uuid.UUID, str]:
    """Each lead's stage: the stored one, or the later stage its records show."""
    leads = list(leads)
    out = {lead.id: lead.stage for lead in leads}
    ready = [lead.id for lead in leads if lead.stage == LeadStage.SPACE_SELECTED]
    if not ready:
        return out
    found = session.execute(
        select(
            Proposal.lead_id,
            TenantOccupancy.id,
            TenantOccupancy.tenant_id,
            TenantOccupancy.category,
        )
        .join(TenantOccupancy, TenantOccupancy.proposal_id == Proposal.id)
        .where(
            Proposal.lead_id.in_(ready),
            TenantOccupancy.status != ContractStatus.CANCELLED,
        )
    ).all()
    if not found:
        return out
    # L-4: the main contract drives the stage, so a lead's Parking contracts count only
    # when it has no other contract (L-5: a parking-only lead follows its Parking contract).
    has_main = {r[0] for r in found if r[3] != ContractCategory.PARKING}
    rows = [
        (lead_id, contract_id, tenant_id)
        for lead_id, contract_id, tenant_id, category in found
        if lead_id not in has_main or category != ContractCategory.PARKING
    ]
    # Approved (ACTIVE, or TERMINATED later) with a signed agreement.
    completed = set(
        session.scalars(
            select(TenantOccupancy.id).where(
                TenantOccupancy.id.in_({r[1] for r in rows}),
                TenantOccupancy.status.in_(
                    (ContractStatus.ACTIVE.value, ContractStatus.TERMINATED.value)
                ),
                select(ContractDocument.id)
                .where(
                    ContractDocument.contract_id == TenantOccupancy.id,
                    ContractDocument.status == DocumentStatus.SIGNED,
                )
                .exists(),
            )
        )
    )
    activated = set(
        session.scalars(
            select(Tenant.id).where(
                Tenant.id.in_({r[2] for r in rows}), Tenant.client_activated_at.is_not(None)
            )
        )
    )
    for lead_id, contract_id, tenant_id in rows:
        stage = CONTRACT_PREPARED
        if contract_id in completed:
            stage = TENANT_ACTIVATED if tenant_id in activated else CONTRACT_COMPLETED
        if PIPELINE.index(stage) > PIPELINE.index(out[lead_id]):
            out[lead_id] = stage
    return out


def _last_recorded(session: Session, lead_id: uuid.UUID) -> str | None:
    message = session.scalar(
        select(ActivityLog.message)
        .where(
            ActivityLog.entity_type == "leads",
            ActivityLog.entity_id == lead_id,
            ActivityLog.action == ACTION,
        )
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(1)
    )
    if not message:
        return None
    return next((code for code, label in LABEL.items() if f"to {label} " in message), None)


def record_progress(
    session: Session, actor: AppUser | None, lead_ids: Iterable[uuid.UUID], cause: str
) -> None:
    """Write a history entry for each lead whose automatic stage changed (forward, or back
    when a contract is cancelled), once per change. Called in the same transaction as the
    change that caused it; never commits."""
    session.flush()
    leads = list(session.scalars(select(Lead).where(Lead.id.in_(set(lead_ids)))))
    stages = pipeline_stages(session, leads)
    for lead in leads:
        if lead.stage != LeadStage.SPACE_SELECTED:
            continue
        now = stages[lead.id]
        before = _last_recorded(session, lead.id) or LeadStage.SPACE_SELECTED.value
        if now == before:
            continue
        log_activity(
            session,
            actor,
            action=ACTION,
            entity_type="leads",
            entity_id=lead.id,
            property_id=lead.property_id,
            message=f"Lead {lead.reference} moved from {LABEL[before]} to {LABEL[now]} "
            f"automatically ({cause}).",
        )


def leads_of_contracts(session: Session, contract_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = set(contract_ids)
    if not ids:
        return set()
    return set(
        session.scalars(
            select(Proposal.lead_id)
            .join(TenantOccupancy, TenantOccupancy.proposal_id == Proposal.id)
            .where(TenantOccupancy.id.in_(ids))
        )
    )


def leads_of_tenant(session: Session, tenant_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        session.scalars(
            select(Proposal.lead_id)
            .join(TenantOccupancy, TenantOccupancy.proposal_id == Proposal.id)
            .where(TenantOccupancy.tenant_id == tenant_id)
        )
    )
