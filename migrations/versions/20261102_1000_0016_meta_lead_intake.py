"""Meta Ads lead intake (Phase 3B-2, polling; BR-008)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-02 10:00:00.000000+00:00

Additive. Two new tables; nothing existing is touched.

- meta_lead_forms: the Meta lead forms imported from, each with the field mapping an admin
  set (Meta question name -> lead field), whether it is enabled, and its last sync.
- meta_lead_events: every lead fetched from Meta, once (unique Meta lead id), with its
  answers, status (RECEIVED, IMPORTED, FAILED), attempts, last error, duplicate warnings
  and the CRM lead created from it (at most one).

The downgrade refuses while any form or received lead is recorded. Hand-written;
tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'RECEIVED', 'IMPORTED', 'FAILED'"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["app_users.id"], name=op.f(f"fk_{table}_{column}_app_users"), ondelete="SET NULL"
    )


def upgrade() -> None:
    t = "meta_lead_forms"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("form_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "field_mapping",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_lead_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        _user_fk(t, "created_by"),
        _user_fk(t, "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("form_id", name=op.f(f"uq_{t}_form_id")),
    )

    t = "meta_lead_events"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("leadgen_id", sa.String(length=64), nullable=False),
        sa.Column("form_ref", sa.Uuid(), nullable=False),
        sa.Column("ad_id", sa.String(length=64), nullable=True),
        sa.Column("adset_id", sa.String(length=64), nullable=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("meta_created_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="RECEIVED", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "duplicates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f(f"ck_{t}_status_valid")),
        sa.CheckConstraint(
            "(status = 'IMPORTED') = (lead_id IS NOT NULL)", name=op.f(f"ck_{t}_imported_has_lead")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f(f"ck_{t}_attempts_not_negative")),
        sa.ForeignKeyConstraint(
            ["form_ref"],
            ["meta_lead_forms.id"],
            name=op.f(f"fk_{t}_form_ref_meta_lead_forms"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f(f"fk_{t}_lead_id_leads"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("leadgen_id", name=op.f(f"uq_{t}_leadgen_id")),
        sa.UniqueConstraint("lead_id", name=op.f(f"uq_{t}_lead_id")),
    )
    op.create_index(op.f(f"ix_{t}_form_ref"), t, ["form_ref"])
    op.create_index(op.f(f"ix_{t}_status"), t, ["status"])


def downgrade() -> None:
    # Like 0014 and 0015: refuse rather than silently discard what was received from Meta.
    op.execute(
        """
        DO $$
        DECLARE forms integer; events integer;
        BEGIN
          SELECT count(*) INTO forms FROM meta_lead_forms;
          SELECT count(*) INTO events FROM meta_lead_events;
          IF forms + events > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0016: % Meta form(s) and % Meta lead(s) are recorded. '
              'Export or clear them deliberately first; this migration will not discard '
              'them.', forms, events;
          END IF;
        END $$;
        """
    )
    op.drop_index(op.f("ix_meta_lead_events_status"), table_name="meta_lead_events")
    op.drop_index(op.f("ix_meta_lead_events_form_ref"), table_name="meta_lead_events")
    op.drop_table("meta_lead_events")
    op.drop_table("meta_lead_forms")
