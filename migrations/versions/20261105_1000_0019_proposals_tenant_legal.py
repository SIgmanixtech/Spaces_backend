"""Proposals, lead-tenant link and tenant legal details (Phase 4A, BR-006, BR-007)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-05 10:00:00.000000+00:00

Additive. Two new tables, one new sequence and new nullable columns; no existing row,
constraint or column is changed, and every new column starts empty.

- proposals: reference PR-YYYY-NNNNNN from proposal_reference_seq; the lead and property
  (both RESTRICT); status DRAFT/SENT/ACCEPTED/REJECTED/WITHDRAWN; dates, rent, deposit,
  currency and free-text charges and terms as entered (nothing calculated); acceptance,
  rejection and withdrawal must be recorded with their status; at most one ACCEPTED
  proposal per lead (partial unique index).
- proposal_spaces: a space offered in a proposal; composite keys make the space belong to
  the proposal's own property. Removed with its proposal.
- leads.tenant_id (RESTRICT, indexed), tenant_linked_at, tenant_linked_by: the tenant
  record a lead belongs to. Several leads may share one tenant.
- tenants: registered_address, signatory_name, signatory_designation, signatory_address,
  gstin, pan, cin. No Aadhaar.

Nothing here creates a contract, allocation, booking, account or tenant.

The downgrade refuses while any proposal, lead-tenant link or tenant legal detail exists,
like the earlier revisions. Hand-written; tests/test_migrations.py checks it against the
models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'DRAFT', 'SENT', 'ACCEPTED', 'REJECTED', 'WITHDRAWN'"
TENANT_COLUMNS = (
    ("registered_address", 500),
    ("signatory_name", 200),
    ("signatory_designation", 120),
    ("signatory_address", 500),
    ("gstin", 15),
    ("pan", 10),
    ("cin", 21),
)


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["app_users.id"], name=op.f(f"fk_{table}_{column}_app_users"), ondelete="SET NULL"
    )


def upgrade() -> None:
    op.execute("CREATE SEQUENCE proposal_reference_seq")

    t = "proposals"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "reference",
            sa.String(length=20),
            server_default=sa.text(
                "('PR-' || to_char(now(), 'YYYY') || '-' "
                "|| lpad(nextval('proposal_reference_seq')::text, 6, '0'))"
            ),
            nullable=False,
        ),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="DRAFT", nullable=False),
        sa.Column("proposal_date", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("monthly_rent", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("security_deposit", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("other_charges", sa.Text(), nullable=True),
        sa.Column("commercial_terms", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_on", sa.Date(), nullable=True),
        sa.Column("acceptance_note", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.Uuid(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f(f"ck_{t}_status_valid")),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date", name=op.f(f"ck_{t}_dates_ordered")
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until >= proposal_date",
            name=op.f(f"ck_{t}_validity_ordered"),
        ),
        sa.CheckConstraint(
            "(monthly_rent IS NULL OR monthly_rent >= 0)"
            " AND (security_deposit IS NULL OR security_deposit >= 0)",
            name=op.f(f"ck_{t}_amounts_not_negative"),
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f(f"ck_{t}_currency_code")),
        sa.CheckConstraint(
            "(status = 'ACCEPTED') = (accepted_on IS NOT NULL AND acceptance_note IS NOT NULL"
            " AND accepted_at IS NOT NULL)",
            name=op.f(f"ck_{t}_acceptance_recorded"),
        ),
        sa.CheckConstraint(
            "(status = 'REJECTED') = (rejected_at IS NOT NULL)",
            name=op.f(f"ck_{t}_rejection_recorded"),
        ),
        sa.CheckConstraint(
            "(status = 'WITHDRAWN') = (withdrawn_at IS NOT NULL)",
            name=op.f(f"ck_{t}_withdrawal_recorded"),
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f(f"fk_{t}_lead_id_leads"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name=op.f(f"fk_{t}_property_id_properties"),
            ondelete="RESTRICT",
        ),
        _user_fk(t, "accepted_by"),
        _user_fk(t, "created_by"),
        _user_fk(t, "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("reference", name=op.f(f"uq_{t}_reference")),
        sa.UniqueConstraint("id", "property_id", name="uq_proposals_id_property"),
    )
    for column in ("lead_id", "property_id", "status"):
        op.create_index(op.f(f"ix_{t}_{column}"), t, [column])
    op.create_index(
        "uq_proposals_accepted_lead",
        t,
        ["lead_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACCEPTED'"),
    )

    t = "proposal_spaces"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        _now("created_at"),
        sa.ForeignKeyConstraint(
            ["proposal_id", "property_id"],
            ["proposals.id", "proposals.property_id"],
            name=op.f(f"fk_{t}_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            name=op.f(f"fk_{t}_space_id_spaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("proposal_id", "space_id", name="uq_proposal_spaces_proposal_space"),
    )
    for column in ("proposal_id", "space_id"):
        op.create_index(op.f(f"ix_{t}_{column}"), t, [column])

    t = "leads"
    op.add_column(t, sa.Column("tenant_id", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("tenant_linked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("tenant_linked_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{t}_tenant_id_tenants"), t, "tenants", ["tenant_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        op.f(f"fk_{t}_tenant_linked_by_app_users"),
        t,
        "app_users",
        ["tenant_linked_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f(f"ix_{t}_tenant_id"), t, ["tenant_id"])

    for column, length in TENANT_COLUMNS:
        op.add_column("tenants", sa.Column(column, sa.String(length=length), nullable=True))


def downgrade() -> None:
    # Like the earlier revisions: refuse rather than silently discard what admins entered.
    filled = " OR ".join(f"{column} IS NOT NULL" for column, _ in TENANT_COLUMNS)
    op.execute(
        f"""
        DO $$
        DECLARE proposals integer; links integer; legal integer;
        BEGIN
          SELECT count(*) INTO proposals FROM proposals;
          SELECT count(*) INTO links FROM leads WHERE tenant_id IS NOT NULL;
          SELECT count(*) INTO legal FROM tenants WHERE {filled};
          IF proposals + links + legal > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0019: % proposal(s), % lead-tenant link(s) and % tenant(s) '
              'with legal details exist. Export or clear them deliberately first; this '
              'migration will not discard them.', proposals, links, legal;
          END IF;
        END $$;
        """
    )
    for column, _ in reversed(TENANT_COLUMNS):
        op.drop_column("tenants", column)
    op.drop_index(op.f("ix_leads_tenant_id"), table_name="leads")
    op.drop_constraint(op.f("fk_leads_tenant_linked_by_app_users"), "leads", type_="foreignkey")
    op.drop_constraint(op.f("fk_leads_tenant_id_tenants"), "leads", type_="foreignkey")
    op.drop_column("leads", "tenant_linked_by")
    op.drop_column("leads", "tenant_linked_at")
    op.drop_column("leads", "tenant_id")
    op.drop_table("proposal_spaces")
    op.drop_table("proposals")
    op.execute("DROP SEQUENCE proposal_reference_seq")
