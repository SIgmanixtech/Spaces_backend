"""Addenda: changes to the terms of a contract, on the same contract (owner decisions LC-3
and LC-4 of 2 Nov 2026).

    DRAFT --sign (signed copy, both parties)--> SIGNED   (new terms applied to the contract)
      +---cancel (reason)----------------------> CANCELLED

A draft changes nothing: until it is signed by both the tenant and SIGMANIX (D11.3) the
contract's existing terms apply, so an unsigned addendum never silently replaces a signed
agreement. Signing applies ``changes`` to the contract and keeps the replaced values in
``previous``; the signed agreement and its copies stay as they are. Dates are not changed
here: a date-only change is an extension, a new term is a renewal.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.agreement import ContractAddendum
from app.models.enums import AddendumStatus, ContractStatus
from app.models.tenant import TenantOccupancy
from app.models.user import AppUser
from app.services.agreements import MAX_SIGNED_COPY_BYTES, PDF, check_signers
from app.services.base import commit, flush, log_activity
from app.storage import ObjectStorage

# What an addendum may change, and how each value is read.
MONEY = ("monthly_rent", "security_deposit")
COUNTS = ("expected_headcount", "occupants")
TEXTS = {
    "terms": 20_000,
    "lock_in_period": 100,
    "notice_period": 100,
    "government_department": 200,
    "government_order_reference": 100,
    "vehicle_registration": 20,
}
CHANGEABLE = (*MONEY, *COUNTS, *TEXTS)


def clean_changes(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - set(CHANGEABLE))
    if unknown:
        raise BadRequestError(
            "An addendum changes terms only: "
            + ", ".join(CHANGEABLE)
            + f". Not: {', '.join(unknown)}. Dates change by extension or renewal."
        )
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or value == "":
            out[key] = None
        elif key in MONEY:
            try:
                amount = Decimal(str(value))
            except InvalidOperation as exc:
                raise BadRequestError(f"{key.replace('_', ' ')} must be an amount.") from exc
            if amount < 0 or amount >= Decimal("10000000000"):
                raise BadRequestError(f"{key.replace('_', ' ')} is out of range.")
            out[key] = str(amount.quantize(Decimal("0.01")))
        elif key in COUNTS:
            if not str(value).isdigit() or int(value) <= 0:
                raise BadRequestError(f"{key.replace('_', ' ')} must be a whole number above 0.")
            out[key] = int(value)
        else:
            text = str(value).strip()
            if len(text) > TEXTS[key]:
                raise BadRequestError(f"{key.replace('_', ' ')} is too long.")
            out[key] = text
    if not out:
        raise BadRequestError("Say what the addendum changes.")
    return out


class AddendumService:
    def __init__(
        self, session: Session, actor: AppUser | None, storage: ObjectStorage | None = None
    ) -> None:
        self.session = session
        self.actor = actor
        self.storage = storage

    def list(self, contract_id: uuid.UUID) -> list[ContractAddendum]:
        self._contract(contract_id)
        return list(
            self.session.scalars(
                select(ContractAddendum)
                .where(ContractAddendum.contract_id == contract_id)
                .order_by(ContractAddendum.created_at, ContractAddendum.id)
            )
        )

    def create(
        self, contract_id: uuid.UUID, effective_date: date, summary: str, changes: dict[str, Any]
    ) -> ContractAddendum:
        contract = self._contract(contract_id, lock=True)
        if contract.status != ContractStatus.ACTIVE:
            raise ConflictError(
                "Addenda are for approved contracts. Change a pending contract directly."
            )
        if contract.end_date is not None and effective_date > contract.end_date:
            raise BadRequestError("The addendum takes effect after the contract ends.")
        summary = " ".join((summary or "").split())
        if not summary:
            raise BadRequestError("Describe the change in a few words.")
        addendum = ContractAddendum(
            contract_id=contract.id,
            status=AddendumStatus.DRAFT,
            effective_date=effective_date,
            summary=summary[:5000],
            changes=clean_changes(changes),
            created_by=self.actor.id if self.actor else None,
        )
        self.session.add(addendum)
        flush(self.session)
        self.session.refresh(addendum, ["reference"])
        self._log(
            "addendum_created",
            contract,
            f"Addendum {addendum.reference} drafted for contract {contract.reference}: "
            f"{summary[:200]}. The current terms apply until it is signed.",
        )
        commit(self.session)
        return addendum

    def cancel(self, addendum_id: uuid.UUID, reason: str) -> ContractAddendum:
        addendum = self._addendum(addendum_id)
        if addendum.status != AddendumStatus.DRAFT:
            raise ConflictError(f"A {addendum.status.lower()} addendum cannot be cancelled.")
        reason = " ".join((reason or "").split())
        if not reason:
            raise BadRequestError("Give a reason for cancelling the addendum.")
        addendum.status = AddendumStatus.CANCELLED
        addendum.cancelled_at = datetime.now(UTC)
        addendum.cancellation_reason = reason[:2000]
        contract = self._contract(addendum.contract_id)
        self._log(
            "addendum_cancelled",
            contract,
            f"Addendum {addendum.reference} cancelled: {reason[:200]}.",
        )
        commit(self.session)
        return addendum

    def sign(
        self,
        addendum_id: uuid.UUID,
        *,
        data: bytes,
        file_name: str,
        signed_on: date,
        signers: list[dict[str, Any]],
    ) -> ContractAddendum:
        """Record the paper-signed addendum (both parties) and apply its changes."""
        addendum = self._addendum(addendum_id)
        if addendum.status != AddendumStatus.DRAFT:
            raise ConflictError(f"A {addendum.status.lower()} addendum cannot be signed.")
        if len(data) > MAX_SIGNED_COPY_BYTES:
            raise BadRequestError("The signed copy is larger than 20 MB.")
        if not data.startswith(b"%PDF-"):
            raise BadRequestError("Upload the signed addendum as a PDF file.")
        clean = check_signers(signers, signed_on)
        contract = self._contract(addendum.contract_id, lock=True)
        if contract.status != ContractStatus.ACTIVE:
            raise ConflictError(
                f"Contract {contract.reference} is {contract.status.lower()}; its addendum "
                "can no longer be applied."
            )
        if self.storage is None:
            raise ConflictError("File storage is not configured on the server.")
        path = f"contracts/addenda/{contract.id}/{addendum.id}.pdf"
        self.storage.put(path, data, PDF)
        previous: dict[str, Any] = {}
        for key, value in addendum.changes.items():
            old = getattr(contract, key)
            previous[key] = str(old) if isinstance(old, Decimal) else old
            setattr(contract, key, Decimal(value) if key in MONEY and value is not None else value)
        addendum.previous = previous
        addendum.bucket = self.storage.bucket
        addendum.object_path = path
        addendum.file_name = (file_name or "addendum.pdf")[-255:]
        addendum.size_bytes = len(data)
        addendum.sha256 = hashlib.sha256(data).hexdigest()
        addendum.signed_on = signed_on
        addendum.signers = clean
        addendum.status = AddendumStatus.SIGNED
        addendum.signed_at = datetime.now(UTC)
        names = ", ".join(s["name"] for s in clean)
        self._log(
            "addendum_signed",
            contract,
            f"Addendum {addendum.reference} signed on {signed_on} by {names}; its terms now "
            f"apply to contract {contract.reference} from {addendum.effective_date}.",
        )
        commit(self.session)
        return addendum

    def link(self, addendum_id: uuid.UUID, expires_in: int = 300) -> str:
        addendum = self._addendum(addendum_id)
        if addendum.object_path is None or self.storage is None:
            raise NotFoundError("This addendum has no signed copy.")
        return self.storage.signed_url(addendum.object_path, expires_in).url

    def _contract(self, contract_id: uuid.UUID, *, lock: bool = False) -> TenantOccupancy:
        statement = select(TenantOccupancy).where(TenantOccupancy.id == contract_id)
        contract = self.session.scalar(statement.with_for_update() if lock else statement)
        if contract is None:
            raise NotFoundError("Contract not found.")
        return contract

    def _addendum(self, addendum_id: uuid.UUID) -> ContractAddendum:
        addendum = self.session.scalar(
            select(ContractAddendum).where(ContractAddendum.id == addendum_id).with_for_update()
        )
        if addendum is None:
            raise NotFoundError("Addendum not found.")
        return addendum

    def _log(self, action: str, contract: TenantOccupancy, message: str) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type="contracts",
            entity_id=contract.id,
            property_id=contract.property_id,
            message=message,
        )
