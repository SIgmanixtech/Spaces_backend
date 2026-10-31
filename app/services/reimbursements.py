"""Reimbursement Fee (business rule BR-029).

Expense types are a catalogue admins extend. Terms say how a type is charged: a property
default (no contract) or a contract's agreed term. Defaults never change a contract by
themselves; `apply_defaults` copies only the types a contract does not have yet. Actual
expenses are entered per contract and period and stay PENDING_REVIEW until reviewed; only
REVIEWED ones are billable (`billable`), which is what invoicing (Phase 6) will use.

Tax is the term's or expense's own treatment and rate; the Space Fee's tax is never
copied. No percentage or per-sq-ft reimbursement exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.building import Building
from app.models.enums import (
    BillingPeriod,
    ReimbursementMethod,
    ReimbursementRecurrence,
    ReimbursementStatus,
    TaxTreatment,
)
from app.models.file import StoredFile
from app.models.pricing import PricingRule
from app.models.property import Property
from app.models.reimbursement import (
    ReimbursementExpense,
    ReimbursementExpenseType,
    ReimbursementTerm,
)
from app.models.space import Space
from app.models.tenant import TenantOccupancy
from app.models.user import AppUser
from app.schemas.reimbursement import (
    AdditionalChargeReview,
    ExpenseRead,
    ExpenseTypeRead,
    TermRead,
)
from app.services.base import commit, flush, log_activity, require

CENT = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def tax_on(amount: Decimal, treatment: str, rate: Decimal | None) -> tuple[Decimal, Decimal]:
    """(tax, total) for an amount, as the pricing engine treats tax: ADDITIONAL adds it,
    INCLUDED shows the part already inside the amount, EXEMPT has none."""
    r = (rate or Decimal(0)) / 100
    if TaxTreatment(treatment) == TaxTreatment.ADDITIONAL:
        tax = _round(amount * r)
        return tax, amount + tax
    if TaxTreatment(treatment) == TaxTreatment.INCLUDED:
        return _round(amount - amount / (1 + r)), amount
    return Decimal(0), amount


# The approved starting catalogue (BR-029). Migration 0012 seeds the same list; this is
# for databases emptied wholesale (the test suites and the local e2e server).
STARTING_EXPENSE_TYPES = (
    ("Electricity", None),
    ("Water", None),
    ("Internet", None),
    ("Diesel generator / power backup", None),
    ("Waste management", None),
    (
        "Other shared operational expenses",
        "Other shared operational expenses paid by SIGMANIX on behalf of tenants.",
    ),
)


def seed_expense_types(session: Session) -> int:
    """Adds any missing starting type; never changes or removes existing ones."""
    have = {n.lower() for n in session.scalars(select(ReimbursementExpenseType.name))}
    added = 0
    for name, description in STARTING_EXPENSE_TYPES:
        if name.lower() not in have:
            session.add(ReimbursementExpenseType(name=name, description=description))
            added += 1
    session.flush()
    return added


def _name(raw: str) -> str:
    return " ".join(raw.split())


@dataclass(frozen=True)
class FeeLine:
    """One Reimbursement Fee line of an estimate."""

    expense_type: str
    amount: Decimal
    tax: Decimal
    tax_included: bool
    total: Decimal


def default_fee_lines(session: Session, property_id: uuid.UUID) -> dict[str, list[FeeLine]]:
    """The property's FIXED, RECURRING default reimbursements, by billing period. These are
    what a space of the property is charged per period under the defaults; actual
    expenses are entered per contract and are not estimated."""
    rows = session.execute(
        select(ReimbursementTerm, ReimbursementExpenseType.name)
        .join(
            ReimbursementExpenseType,
            ReimbursementExpenseType.id == ReimbursementTerm.expense_type_id,
        )
        .where(
            ReimbursementTerm.property_id == property_id,
            ReimbursementTerm.contract_id.is_(None),
            ReimbursementTerm.method == ReimbursementMethod.FIXED,
            ReimbursementTerm.recurrence == ReimbursementRecurrence.RECURRING,
            ReimbursementExpenseType.is_active.is_(True),
        )
        .order_by(ReimbursementExpenseType.name)
    ).all()
    out: dict[str, list[FeeLine]] = {}
    for term, type_name in rows:
        amount = Decimal(term.amount)
        tax, total = tax_on(amount, term.tax_treatment, term.tax_rate)
        out.setdefault(term.billing_period, []).append(
            FeeLine(
                expense_type=type_name,
                amount=amount,
                tax=tax,
                tax_included=term.tax_treatment == TaxTreatment.INCLUDED,
                total=total,
            )
        )
    return out


class ReimbursementService:
    def __init__(self, session: Session, actor: AppUser | None) -> None:
        self.session = session
        self.actor = actor

    def _log(
        self, action: str, entity: str, entity_id: uuid.UUID, prop: uuid.UUID | None, msg: str
    ) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type=entity,
            entity_id=entity_id,
            property_id=prop,
            message=msg,
        )

    # --- expense types ----------------------------------------------------------------

    def types(self, include_inactive: bool = True) -> list[ExpenseTypeRead]:
        query = select(ReimbursementExpenseType).order_by(ReimbursementExpenseType.name)
        if not include_inactive:
            query = query.where(ReimbursementExpenseType.is_active.is_(True))
        return [ExpenseTypeRead.model_validate(t) for t in self.session.scalars(query)]

    def _type_named(
        self, name: str, exclude: uuid.UUID | None = None
    ) -> ReimbursementExpenseType | None:
        query = select(ReimbursementExpenseType).where(
            func.lower(ReimbursementExpenseType.name) == name.lower()
        )
        if exclude is not None:
            query = query.where(ReimbursementExpenseType.id != exclude)
        return self.session.scalar(query)

    def create_type(self, name: str, description: str | None) -> ExpenseTypeRead:
        name = _name(name)
        if not name:
            raise BadRequestError("Give the expense type a name.")
        if self._type_named(name) is not None:
            raise ConflictError(f'An expense type named "{name}" already exists.')
        made = ReimbursementExpenseType(
            name=name, description=description, created_by=self.actor.id if self.actor else None
        )
        self.session.add(made)
        flush(self.session)
        self._log(
            "created", "reimbursement_expense_types", made.id, None, f'Expense type "{name}" added.'
        )
        commit(self.session)
        return ExpenseTypeRead.model_validate(made)

    def update_type(self, type_id: uuid.UUID, values: dict[str, Any]) -> ExpenseTypeRead:
        found = require(self.session, ReimbursementExpenseType, type_id, "Expense type")
        if "name" in values:
            name = _name(values["name"])
            if not name:
                raise BadRequestError("Give the expense type a name.")
            if self._type_named(name, exclude=found.id) is not None:
                raise ConflictError(f'An expense type named "{name}" already exists.')
            values["name"] = name
        before = found.name
        changes = []
        for key, value in values.items():
            old = getattr(found, key)
            if old != value:
                changes.append(f"{key} {old} -> {value}")
                setattr(found, key, value)
        if changes:
            self._log(
                "updated",
                "reimbursement_expense_types",
                found.id,
                None,
                f'Expense type "{before}" updated: ' + "; ".join(changes) + ".",
            )
        commit(self.session)
        return ExpenseTypeRead.model_validate(found)

    def _active_type(self, type_id: uuid.UUID) -> ReimbursementExpenseType:
        found = require(self.session, ReimbursementExpenseType, type_id, "Expense type")
        if not found.is_active:
            raise BadRequestError(f'The expense type "{found.name}" is no longer in use.')
        return found

    # --- terms ------------------------------------------------------------------------

    def _contract(
        self, contract_id: uuid.UUID, property_id: uuid.UUID | None = None
    ) -> TenantOccupancy:
        contract = require(self.session, TenantOccupancy, contract_id, "Contract")
        if property_id is not None and contract.property_id != property_id:
            raise BadRequestError("The contract does not belong to this property.")
        return contract

    @staticmethod
    def _checked_terms(v: dict[str, Any]) -> dict[str, Any]:
        method = ReimbursementMethod(v["method"])
        if method == ReimbursementMethod.FIXED and v.get("amount") is None:
            raise BadRequestError("A fixed reimbursement needs an amount.")
        if method == ReimbursementMethod.ACTUAL and v.get("amount") is not None:
            raise BadRequestError(
                "An actual reimbursement has no fixed amount: enter each period's expense."
            )
        recurrence = ReimbursementRecurrence(v["recurrence"])
        period = v.get("billing_period")
        if recurrence == ReimbursementRecurrence.RECURRING:
            if period is None:
                raise BadRequestError("A recurring reimbursement needs a billing period.")
            if BillingPeriod(period) == BillingPeriod.CUSTOM:
                raise BadRequestError("Choose a standard billing period for a reimbursement.")
        else:
            v["billing_period"] = None
        if TaxTreatment(v["tax_treatment"]) == TaxTreatment.EXEMPT:
            v["tax_rate"] = None
        elif v.get("tax_rate") is None:
            raise BadRequestError("Give the tax rate, or choose EXEMPT.")
        return v

    def terms(self, property_id: uuid.UUID, contract_id: uuid.UUID | None = None) -> list[TermRead]:
        require(self.session, Property, property_id, "Property")
        query = select(ReimbursementTerm).where(ReimbursementTerm.property_id == property_id)
        if contract_id is None:
            query = query.where(ReimbursementTerm.contract_id.is_(None))
        else:
            self._contract(contract_id, property_id)
            query = query.where(ReimbursementTerm.contract_id == contract_id)
        return self.term_reads(list(self.session.scalars(query)))

    def term_reads(self, terms: list[ReimbursementTerm]) -> list[TermRead]:
        names = (
            dict(
                self.session.execute(
                    select(ReimbursementExpenseType.id, ReimbursementExpenseType.name).where(
                        ReimbursementExpenseType.id.in_({t.expense_type_id for t in terms})
                    )
                ).all()
            )
            if terms
            else {}
        )
        out = []
        for t in sorted(terms, key=lambda t: names.get(t.expense_type_id, "")):
            fixed = t.amount is not None
            tax, total = (
                tax_on(Decimal(t.amount), t.tax_treatment, t.tax_rate) if fixed else (None, None)
            )
            out.append(
                TermRead(
                    id=t.id,
                    property_id=t.property_id,
                    contract_id=t.contract_id,
                    scope="CONTRACT" if t.contract_id else "PROPERTY_DEFAULT",
                    expense_type_id=t.expense_type_id,
                    expense_type_name=names.get(t.expense_type_id, ""),
                    method=ReimbursementMethod(t.method),
                    amount=float(t.amount) if fixed else None,
                    recurrence=ReimbursementRecurrence(t.recurrence),
                    billing_period=BillingPeriod(t.billing_period) if t.billing_period else None,
                    tax_treatment=TaxTreatment(t.tax_treatment),
                    tax_rate=float(t.tax_rate) if t.tax_rate is not None else None,
                    currency=t.currency,
                    notes=t.notes,
                    tax=float(tax) if tax is not None else None,
                    total=float(total) if total is not None else None,
                    created_at=t.created_at,
                    updated_at=t.updated_at,
                )
            )
        return out

    def _duplicate_term(
        self,
        property_id: uuid.UUID,
        contract_id: uuid.UUID | None,
        type_id: uuid.UUID,
        exclude: uuid.UUID | None = None,
    ) -> bool:
        query = select(ReimbursementTerm.id).where(
            ReimbursementTerm.property_id == property_id,
            ReimbursementTerm.expense_type_id == type_id,
            ReimbursementTerm.contract_id.is_(None)
            if contract_id is None
            else ReimbursementTerm.contract_id == contract_id,
        )
        if exclude is not None:
            query = query.where(ReimbursementTerm.id != exclude)
        return self.session.scalar(query) is not None

    def create_term(self, values: dict[str, Any]) -> TermRead:
        require(self.session, Property, values["property_id"], "Property")
        if values.get("contract_id") is not None:
            self._contract(values["contract_id"], values["property_id"])
        expense_type = self._active_type(values["expense_type_id"])
        if self._duplicate_term(values["property_id"], values.get("contract_id"), expense_type.id):
            where = "This contract" if values.get("contract_id") else "This property"
            raise ConflictError(f'{where} already has a "{expense_type.name}" reimbursement.')
        values = self._checked_terms(dict(values))
        term = ReimbursementTerm(
            **values,
            created_by=self.actor.id if self.actor else None,
            updated_by=self.actor.id if self.actor else None,
        )
        self.session.add(term)
        flush(self.session)
        scope = "contract term" if term.contract_id else "property default"
        self._log(
            "created",
            "reimbursement_terms",
            term.id,
            term.property_id,
            f'Reimbursement {scope} for "{expense_type.name}" added.',
        )
        commit(self.session)
        return self.term_reads([term])[0]

    def update_term(self, term_id: uuid.UUID, values: dict[str, Any]) -> TermRead:
        term = require(self.session, ReimbursementTerm, term_id, "Reimbursement term")
        merged = {
            k: getattr(term, k)
            for k in (
                "method",
                "amount",
                "recurrence",
                "billing_period",
                "tax_treatment",
                "tax_rate",
            )
        }
        merged.update(values)
        if (
            ReimbursementMethod(merged["method"]) == ReimbursementMethod.ACTUAL
            and "amount" not in values
        ):
            merged["amount"] = None
        checked = self._checked_terms(merged)
        for key in (
            "method",
            "amount",
            "recurrence",
            "billing_period",
            "tax_treatment",
            "tax_rate",
        ):
            setattr(term, key, checked[key])
        for key in ("currency", "notes"):
            if key in values:
                setattr(term, key, values[key])
        term.updated_by = self.actor.id if self.actor else None
        self._log(
            "updated",
            "reimbursement_terms",
            term.id,
            term.property_id,
            "Reimbursement term updated.",
        )
        commit(self.session)
        return self.term_reads([term])[0]

    def delete_term(self, term_id: uuid.UUID) -> None:
        term = require(self.session, ReimbursementTerm, term_id, "Reimbursement term")
        self._log(
            "deleted",
            "reimbursement_terms",
            term.id,
            term.property_id,
            "Reimbursement term removed.",
        )
        self.session.delete(term)
        commit(self.session)

    def apply_defaults(self, contract_id: uuid.UUID) -> tuple[list[TermRead], list[str]]:
        """Copies the property's defaults onto the contract, for types it does not have yet.
        Existing contract terms are never changed."""
        contract = self._contract(contract_id)
        defaults = list(
            self.session.scalars(
                select(ReimbursementTerm).where(
                    ReimbursementTerm.property_id == contract.property_id,
                    ReimbursementTerm.contract_id.is_(None),
                )
            )
        )
        have = set(
            self.session.scalars(
                select(ReimbursementTerm.expense_type_id).where(
                    ReimbursementTerm.contract_id == contract.id
                )
            )
        )
        names = dict(
            self.session.execute(
                select(ReimbursementExpenseType.id, ReimbursementExpenseType.name)
            ).all()
        )
        made, skipped = [], []
        for d in defaults:
            if d.expense_type_id in have:
                skipped.append(names.get(d.expense_type_id, ""))
                continue
            term = ReimbursementTerm(
                property_id=contract.property_id,
                contract_id=contract.id,
                expense_type_id=d.expense_type_id,
                method=d.method,
                amount=d.amount,
                recurrence=d.recurrence,
                billing_period=d.billing_period,
                tax_treatment=d.tax_treatment,
                tax_rate=d.tax_rate,
                currency=d.currency,
                notes=d.notes,
                created_by=self.actor.id if self.actor else None,
                updated_by=self.actor.id if self.actor else None,
            )
            self.session.add(term)
            made.append(term)
        flush(self.session)
        if made:
            self._log(
                "updated",
                "contracts",
                contract.id,
                contract.property_id,
                f"Reimbursement terms copied from the property defaults ({len(made)}).",
            )
        commit(self.session)
        return self.term_reads(made), sorted(skipped)

    # --- actual expenses --------------------------------------------------------------

    def expenses(
        self,
        *,
        contract_id: uuid.UUID | None = None,
        property_id: uuid.UUID | None = None,
        status: ReimbursementStatus | None = None,
    ) -> list[ExpenseRead]:
        query = select(ReimbursementExpense)
        if contract_id is not None:
            query = query.where(ReimbursementExpense.contract_id == contract_id)
        if property_id is not None:
            query = query.where(ReimbursementExpense.property_id == property_id)
        if status is not None:
            query = query.where(ReimbursementExpense.status == status)
        rows = list(
            self.session.scalars(
                query.order_by(
                    ReimbursementExpense.period_start.desc(), ReimbursementExpense.created_at
                )
            )
        )
        return self.expense_reads(rows)

    def expense_reads(self, rows: list[ReimbursementExpense]) -> list[ExpenseRead]:
        if not rows:
            return []
        names = dict(
            self.session.execute(
                select(ReimbursementExpenseType.id, ReimbursementExpenseType.name).where(
                    ReimbursementExpenseType.id.in_({r.expense_type_id for r in rows})
                )
            ).all()
        )
        refs = dict(
            self.session.execute(
                select(TenantOccupancy.id, TenantOccupancy.reference).where(
                    TenantOccupancy.id.in_({r.contract_id for r in rows})
                )
            ).all()
        )
        out = []
        for r in rows:
            tax, total = tax_on(Decimal(r.amount), r.tax_treatment, r.tax_rate)
            out.append(
                ExpenseRead(
                    id=r.id,
                    property_id=r.property_id,
                    contract_id=r.contract_id,
                    contract_reference=refs.get(r.contract_id, ""),
                    expense_type_id=r.expense_type_id,
                    expense_type_name=names.get(r.expense_type_id, ""),
                    period_start=r.period_start,
                    period_end=r.period_end,
                    amount=float(r.amount),
                    original_amount=float(r.original_amount),
                    currency=r.currency,
                    description=r.description,
                    one_time=r.one_time,
                    tax_treatment=TaxTreatment(r.tax_treatment),
                    tax_rate=float(r.tax_rate) if r.tax_rate is not None else None,
                    tax=float(tax),
                    total=float(total),
                    receipt_file_id=r.receipt_file_id,
                    status=ReimbursementStatus(r.status),
                    review_note=r.review_note,
                    reviewed_by=r.reviewed_by,
                    reviewed_at=r.reviewed_at,
                    entered_by=r.entered_by,
                    updated_by=r.updated_by,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
            )
        return out

    def _contract_term(
        self, contract_id: uuid.UUID, type_id: uuid.UUID
    ) -> ReimbursementTerm | None:
        return self.session.scalar(
            select(ReimbursementTerm).where(
                ReimbursementTerm.contract_id == contract_id,
                ReimbursementTerm.expense_type_id == type_id,
            )
        )

    def _receipt(self, file_id: uuid.UUID | None, property_id: uuid.UUID) -> None:
        if file_id is None:
            return
        stored = self.session.get(StoredFile, file_id)
        if stored is None or stored.property_id != property_id:
            raise BadRequestError("The receipt must be a file stored for the same property.")

    def create_expense(self, values: dict[str, Any]) -> ExpenseRead:
        contract = self._contract(values["contract_id"])
        expense_type = self._active_type(values["expense_type_id"])
        term = self._contract_term(contract.id, expense_type.id)
        if not values.get("one_time") and (
            term is None or term.method != ReimbursementMethod.ACTUAL
        ):
            raise BadRequestError(
                f'Contract {contract.reference} has no "actual" reimbursement term for '
                f'"{expense_type.name}". Add one to the contract, or enter this as a '
                "one-time expense."
            )
        if values.get("tax_treatment") is None:
            values["tax_treatment"] = term.tax_treatment if term else TaxTreatment.EXEMPT
            if values.get("tax_rate") is None and term is not None:
                values["tax_rate"] = term.tax_rate
        if TaxTreatment(values["tax_treatment"]) == TaxTreatment.EXEMPT:
            values["tax_rate"] = None
        elif values.get("tax_rate") is None:
            raise BadRequestError("Give the tax rate, or choose EXEMPT.")
        self._receipt(values.get("receipt_file_id"), contract.property_id)
        expense = ReimbursementExpense(
            **{**values, "property_id": contract.property_id},
            original_amount=values["amount"],
            entered_by=self.actor.id if self.actor else None,
            updated_by=self.actor.id if self.actor else None,
        )
        self.session.add(expense)
        flush(self.session)
        self._log(
            "created",
            "reimbursement_expenses",
            expense.id,
            contract.property_id,
            f"{expense_type.name} expense of {expense.currency} {expense.amount} for "
            f"{expense.period_start.isoformat()} to {expense.period_end.isoformat()} entered "
            f"for contract {contract.reference}; awaiting review.",
        )
        commit(self.session)
        return self.expense_reads([expense])[0]

    def _pending(self, expense_id: uuid.UUID) -> ReimbursementExpense:
        expense = self.session.scalar(
            select(ReimbursementExpense)
            .where(ReimbursementExpense.id == expense_id)
            .with_for_update()
        )
        if expense is None:
            raise NotFoundError("Expense not found.")
        if expense.status != ReimbursementStatus.PENDING_REVIEW:
            raise ConflictError(
                f"A {expense.status.lower().replace('_', ' ')} expense cannot be changed."
            )
        return expense

    def update_expense(self, expense_id: uuid.UUID, values: dict[str, Any]) -> ExpenseRead:
        expense = self._pending(expense_id)
        start = values.get("period_start", expense.period_start)
        end = values.get("period_end", expense.period_end)
        if end < start:
            raise BadRequestError("period_end cannot be before period_start.")
        treatment = values.get("tax_treatment", expense.tax_treatment)
        rate = values.get("tax_rate", expense.tax_rate)
        if TaxTreatment(treatment) == TaxTreatment.EXEMPT:
            values["tax_rate"] = None
        elif rate is None:
            raise BadRequestError("Give the tax rate, or choose EXEMPT.")
        if "receipt_file_id" in values:
            self._receipt(values["receipt_file_id"], expense.property_id)
        changes = []
        for key, value in values.items():
            old = getattr(expense, key)
            if old != value:
                changes.append(f"{key} {old} -> {value}")
                setattr(expense, key, value)
        expense.updated_by = self.actor.id if self.actor else None
        if changes:
            self._log(
                "updated",
                "reimbursement_expenses",
                expense.id,
                expense.property_id,
                "Expense corrected before review: " + "; ".join(changes) + ".",
            )
        commit(self.session)
        return self.expense_reads([expense])[0]

    def review(self, expense_id: uuid.UUID, decision: str, note: str | None) -> ExpenseRead:
        expense = self._pending(expense_id)
        if decision == ReimbursementStatus.REJECTED and not (note and note.strip()):
            raise BadRequestError("Say why the expense is rejected.")
        expense.status = decision
        expense.review_note = note.strip() if note else None
        expense.reviewed_by = self.actor.id if self.actor else None
        expense.reviewed_at = datetime.now(UTC)
        self._log(
            "reviewed" if decision == ReimbursementStatus.REVIEWED else "rejected",
            "reimbursement_expenses",
            expense.id,
            expense.property_id,
            f"Expense {decision.lower()}" + (f": {note.strip()}" if note else "."),
        )
        commit(self.session)
        return self.expense_reads([expense])[0]

    def billable(self, contract_id: uuid.UUID, start: date, end: date) -> list[ExpenseRead]:
        """The expenses an invoice for this contract and period may include: REVIEWED only,
        with the whole expense period inside the invoice period."""
        rows = list(
            self.session.scalars(
                select(ReimbursementExpense).where(
                    ReimbursementExpense.contract_id == contract_id,
                    ReimbursementExpense.status == ReimbursementStatus.REVIEWED,
                    ReimbursementExpense.period_start >= start,
                    ReimbursementExpense.period_end <= end,
                )
            )
        )
        return self.expense_reads(rows)

    # --- existing additional charges (read-only report) --------------------------------

    def additional_charges_review(
        self, property_id: uuid.UUID | None = None
    ) -> list[AdditionalChargeReview]:
        """Lists every additional charge on a pricing rule and whether its label matches an
        expense type. Nothing is changed: classifying is the admin's decision."""
        query = select(PricingRule).where(
            func.jsonb_array_length(PricingRule.additional_charges) > 0
        )
        if property_id is not None:
            query = query.where(PricingRule.property_id == property_id)
        types = [t.name for t in self.session.scalars(select(ReimbursementExpenseType))]
        spaces = dict(self.session.execute(select(Space.id, Space.name)).all())
        buildings = dict(self.session.execute(select(Building.id, Building.name)).all())
        out = []
        for rule in self.session.scalars(query.order_by(PricingRule.created_at)):
            target = (
                f"Space {spaces.get(rule.space_id, '')}"
                if rule.space_id
                else f"Building {buildings.get(rule.building_id, '')}"
                if rule.building_id
                else "Property default"
            )
            for charge in rule.additional_charges or []:
                label = str(charge.get("label", ""))
                match = _match_type(label, types)
                out.append(
                    AdditionalChargeReview(
                        rule_id=rule.id,
                        property_id=rule.property_id,
                        rule_status=rule.status,
                        target=target,
                        label=label,
                        amount=float(charge.get("amount", 0)),
                        classification="MATCHES_EXPENSE_TYPE" if match else "UNCLASSIFIED",
                        suggested_expense_type=match,
                        note=(
                            f'Looks like the "{match}" reimbursement. It is still charged as part '
                            "of the space rate; nothing was changed."
                            if match
                            else "Not recognised as a reimbursement. It stays part of the "
                            "space rate."
                        ),
                    )
                )
        return out


def _match_type(label: str, types: list[str]) -> str | None:
    text = label.strip().lower()
    if not text:
        return None
    for name in types:
        n = name.lower()
        first = n.split()[0].strip("/")
        if text == n or (len(first) >= 4 and first in text) or (len(text) >= 4 and text in n):
            return name
    return None
