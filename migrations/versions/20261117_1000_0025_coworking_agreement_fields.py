"""Coworking agreement details: member's personal PAN, fee due day, TDS, company bank details

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-17 10:00:00.000000+00:00

Owner decisions of 17 Nov 2026 for the Coworking License Agreement:

1. tenants.personal_pan: the member's own PAN, kept apart from the company's PAN
   (tenants.pan). Same format check as the company PAN.
2. tenant_occupancies.payment_due_day (1-28): the day of the month the fee is due, as the
   administrator enters it per contract. tenant_occupancies.tds_percent (0-100): the TDS the
   member deducts, entered per agreement; empty means no TDS. Neither is assumed anywhere.
3. company_profiles: phone, email and bank details (bank, account name and number, IFSC,
   branch, UPI ID) of the licensor, printed on agreements from this one place.

Additive: nullable columns and checks only. The downgrade refuses while any of the new
columns holds a value (dropping them would lose it).
Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROFILE_COLUMNS = (
    ("phone", 32),
    ("email", 320),
    ("bank_name", 120),
    ("bank_account_name", 200),
    ("bank_account_number", 34),
    ("bank_ifsc", 11),
    ("bank_branch", 120),
    ("upi_id", 100),
)


def upgrade() -> None:
    op.add_column("tenants", sa.Column("personal_pan", sa.String(10), nullable=True))
    op.create_check_constraint(
        op.f("ck_tenants_personal_pan_format"),
        "tenants",
        "personal_pan IS NULL OR personal_pan ~ '^[A-Z]{5}[0-9]{4}[A-Z]$'",
    )
    op.add_column(
        "tenant_occupancies", sa.Column("payment_due_day", sa.SmallInteger(), nullable=True)
    )
    op.add_column("tenant_occupancies", sa.Column("tds_percent", sa.Numeric(5, 2), nullable=True))
    op.create_check_constraint(
        op.f("ck_tenant_occupancies_due_day_range"),
        "tenant_occupancies",
        "payment_due_day IS NULL OR payment_due_day BETWEEN 1 AND 28",
    )
    op.create_check_constraint(
        op.f("ck_tenant_occupancies_tds_range"),
        "tenant_occupancies",
        "tds_percent IS NULL OR (tds_percent >= 0 AND tds_percent <= 100)",
    )
    for name, length in PROFILE_COLUMNS:
        op.add_column("company_profiles", sa.Column(name, sa.String(length), nullable=True))
    op.create_check_constraint(
        op.f("ck_company_profiles_bank_ifsc_format"),
        "company_profiles",
        "bank_ifsc IS NULL OR bank_ifsc ~ '^[A-Z]{4}0[A-Z0-9]{6}$'",
    )


def downgrade() -> None:
    bind = op.get_bind()
    checks = [
        "SELECT 1 FROM tenants WHERE personal_pan IS NOT NULL LIMIT 1",
        "SELECT 1 FROM tenant_occupancies"
        " WHERE payment_due_day IS NOT NULL OR tds_percent IS NOT NULL LIMIT 1",
        "SELECT 1 FROM company_profiles WHERE "
        + " OR ".join(f"{name} IS NOT NULL" for name, _ in PROFILE_COLUMNS)
        + " LIMIT 1",
    ]
    if any(bind.execute(sa.text(sql)).first() for sql in checks):
        raise RuntimeError(
            "Coworking agreement details are recorded; downgrading 0025 would lose them."
        )
    op.drop_constraint(
        op.f("ck_company_profiles_bank_ifsc_format"), "company_profiles", type_="check"
    )
    for name, _ in reversed(PROFILE_COLUMNS):
        op.drop_column("company_profiles", name)
    op.drop_constraint(op.f("ck_tenant_occupancies_tds_range"), "tenant_occupancies", type_="check")
    op.drop_constraint(
        op.f("ck_tenant_occupancies_due_day_range"), "tenant_occupancies", type_="check"
    )
    op.drop_column("tenant_occupancies", "tds_percent")
    op.drop_column("tenant_occupancies", "payment_due_day")
    op.drop_constraint(op.f("ck_tenants_personal_pan_format"), "tenants", type_="check")
    op.drop_column("tenants", "personal_pan")
