"""CRM core: leads, lead interests, notes and follow-up tasks (Phase 3A, BR-006, BR-008)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-31 15:00:00.000000+00:00

Additive. Four new tables and one new sequence; no existing table, row or constraint is
touched. Leads never become tenants here (conversion is Phase 5), so there is no link to
tenants yet.

- leads: reference LD-YYYY-NNNNNN from lead_reference_seq; source; the three manually set
  stages; company and primary contact; normalised email/phone/company keys for duplicate
  warnings (indexed, deliberately not unique: duplicates are warned about, never refused);
  requirements; owner (an app user).
- lead_interests: a property, or a space of that same property (composite key).
- lead_notes, lead_tasks: notes and follow-ups (due date, done, owner).

The downgrade refuses while any CRM row exists. Hand-written; tests/test_migrations.py
checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = "'MANUAL', 'EXCEL', 'META_ADS', 'OTHER'"
STAGES = "'LEAD_CREATED', 'LEAD_REVIEWED', 'SPACE_SELECTED'"
SPACE_TYPES = (
    "'DESK', 'PRIVATE_OFFICE', 'CABIN', 'MEETING_ROOM', 'COWORKING', 'FLOOR', 'PORTION', "
    "'CONFERENCE_ROOM', 'TRAINING_ROOM', 'NON_RENTABLE', 'DEDICATED_WORKSTATION', "
    "'FLEXIBLE_WORKSTATION', 'SEAT', 'OTHER'"
)
PERIODS = "'HOURLY', 'HALF_DAY', 'FULL_DAY', 'DAILY', 'WEEKLY', 'MONTHLY', 'CUSTOM'"


def _timestamps(updated: bool = True) -> list[sa.Column]:
    columns = [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            )
        )
    return columns


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["app_users.id"], name=op.f(f"fk_{table}_{column}_app_users"), ondelete="SET NULL"
    )


def upgrade() -> None:
    op.execute("CREATE SEQUENCE lead_reference_seq")

    t = "leads"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "reference",
            sa.String(length=20),
            server_default=sa.text(
                "('LD-' || to_char(now(), 'YYYY') || '-' "
                "|| lpad(nextval('lead_reference_seq')::text, 6, '0'))"
            ),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=24), server_default="LEAD_CREATED", nullable=False),
        sa.Column(
            "stage_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column("contact_name", sa.String(length=200), nullable=True),
        sa.Column("contact_email", sa.String(length=320), nullable=True),
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
        sa.Column("email_key", sa.String(length=320), nullable=True),
        sa.Column("phone_key", sa.String(length=20), nullable=True),
        sa.Column("company_key", sa.String(length=200), nullable=True),
        sa.Column("property_id", sa.Uuid(), nullable=True),
        sa.Column("space_type", sa.String(length=32), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("area_sqft", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("budget_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("budget_period", sa.String(length=16), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("desired_start_date", sa.Date(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(f"source IN ({SOURCES})", name=op.f(f"ck_{t}_source_valid")),
        sa.CheckConstraint(f"stage IN ({STAGES})", name=op.f(f"ck_{t}_stage_valid")),
        sa.CheckConstraint(f"space_type IN ({SPACE_TYPES})", name=op.f(f"ck_{t}_space_type_valid")),
        sa.CheckConstraint(
            f"budget_period IN ({PERIODS})", name=op.f(f"ck_{t}_budget_period_valid")
        ),
        sa.CheckConstraint(
            "company_name IS NOT NULL OR contact_name IS NOT NULL", name=op.f(f"ck_{t}_has_name")
        ),
        sa.CheckConstraint(
            "capacity IS NULL OR capacity > 0", name=op.f(f"ck_{t}_capacity_positive")
        ),
        sa.CheckConstraint(
            "area_sqft IS NULL OR area_sqft > 0", name=op.f(f"ck_{t}_area_positive")
        ),
        sa.CheckConstraint(
            "(budget_amount IS NULL) = (budget_period IS NULL)"
            " AND (budget_amount IS NULL OR budget_amount >= 0)",
            name=op.f(f"ck_{t}_budget"),
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f(f"ck_{t}_currency_code")),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name=op.f(f"fk_{t}_property_id_properties"),
            ondelete="RESTRICT",
        ),
        _user_fk(t, "owner_id"),
        _user_fk(t, "created_by"),
        _user_fk(t, "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("reference", name=op.f(f"uq_{t}_reference")),
    )
    for column in (
        "source",
        "stage",
        "email_key",
        "phone_key",
        "company_key",
        "property_id",
        "owner_id",
    ):
        op.create_index(op.f(f"ix_{t}_{column}"), t, [column])

    t = "lead_interests"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_timestamps(updated=False),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f(f"fk_{t}_lead_id_leads"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name=op.f(f"fk_{t}_property_id_properties"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            name=op.f(f"fk_{t}_space_id_spaces"),
            ondelete="RESTRICT",
        ),
        _user_fk(t, "created_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    for column in ("lead_id", "property_id", "space_id"):
        op.create_index(op.f(f"ix_{t}_{column}"), t, [column])
    op.create_index(
        "uq_lead_interests_property",
        t,
        ["lead_id", "property_id"],
        unique=True,
        postgresql_where=sa.text("space_id IS NULL"),
    )
    op.create_index(
        "uq_lead_interests_space",
        t,
        ["lead_id", "space_id"],
        unique=True,
        postgresql_where=sa.text("space_id IS NOT NULL"),
    )

    t = "lead_notes"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f(f"fk_{t}_lead_id_leads"), ondelete="RESTRICT"
        ),
        _user_fk(t, "created_by"),
        _user_fk(t, "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    op.create_index(op.f(f"ix_{t}_lead_id"), t, ["lead_id"])

    t = "lead_tasks"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("done", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "done = (completed_at IS NOT NULL)", name=op.f(f"ck_{t}_completed_when_done")
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f(f"fk_{t}_lead_id_leads"), ondelete="RESTRICT"
        ),
        _user_fk(t, "owner_id"),
        _user_fk(t, "completed_by"),
        _user_fk(t, "created_by"),
        _user_fk(t, "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    op.create_index(op.f(f"ix_{t}_lead_id"), t, ["lead_id"])
    op.create_index(op.f(f"ix_{t}_owner_id"), t, ["owner_id"])
    op.create_index("ix_lead_tasks_done_due", t, ["done", "due_date"])


def downgrade() -> None:
    # Like 0002-0009, 0012 and 0013: refuse rather than silently discard what admins entered.
    op.execute(
        """
        DO $$
        DECLARE leads integer; interests integer; notes integer; tasks integer;
        BEGIN
          SELECT count(*) INTO leads FROM leads;
          SELECT count(*) INTO interests FROM lead_interests;
          SELECT count(*) INTO notes FROM lead_notes;
          SELECT count(*) INTO tasks FROM lead_tasks;
          IF leads + interests + notes + tasks > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0014: % lead(s), % interest(s), % note(s) and % task(s) '
              'exist. Export or clear them deliberately first; this migration will not '
              'discard them.', leads, interests, notes, tasks;
          END IF;
        END $$;
        """
    )
    op.drop_table("lead_tasks")
    op.drop_table("lead_notes")
    op.drop_table("lead_interests")
    op.drop_table("leads")
    op.execute("DROP SEQUENCE lead_reference_seq")
