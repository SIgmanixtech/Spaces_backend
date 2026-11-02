"""Lead import history (Phase 3B-1, BR-008)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-01 10:00:00.000000+00:00

Additive. One new table and one new nullable column; no existing row or constraint is
changed.

- lead_import_batches: one row per confirmed spreadsheet import: the admin who ran it,
  the file name and its SHA-256 fingerprint (the file itself is not kept), the column
  mapping used, the counts and each row's outcome. Previews are not recorded.
- leads.import_batch_id: the import a lead came from (null for leads entered by hand).

The downgrade refuses while any import is recorded or any lead links to one. Hand-written;
tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FILE_TYPES = "'XLSX', 'CSV'"


def upgrade() -> None:
    t = "lead_import_batches"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_type", sa.String(length=8), nullable=False),
        sa.Column(
            "mapping",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column(
            "results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint(f"file_type IN ({FILE_TYPES})", name=op.f(f"ck_{t}_file_type_valid")),
        sa.CheckConstraint(
            "total_rows >= 0 AND imported_count >= 0 AND skipped_count >= 0"
            " AND error_count >= 0 AND duplicate_count >= 0",
            name=op.f(f"ck_{t}_counts_not_negative"),
        ),
        sa.CheckConstraint(
            "imported_count + skipped_count + error_count = total_rows",
            name=op.f(f"ck_{t}_counts_add_up"),
        ),
        sa.CheckConstraint(
            "file_sha256 ~ '^[0-9a-f]{64}$'", name=op.f(f"ck_{t}_file_sha256_hex")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_users.id"],
            name=op.f(f"fk_{t}_created_by_app_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    op.create_index(op.f(f"ix_{t}_file_sha256"), t, ["file_sha256"])

    op.add_column("leads", sa.Column("import_batch_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_leads_import_batch_id_lead_import_batches"),
        "leads",
        t,
        ["import_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_leads_import_batch_id"), "leads", ["import_batch_id"])


def downgrade() -> None:
    # Like 0014: refuse rather than silently discard the record of what was imported.
    op.execute(
        """
        DO $$
        DECLARE batches integer; linked integer;
        BEGIN
          SELECT count(*) INTO batches FROM lead_import_batches;
          SELECT count(*) INTO linked FROM leads WHERE import_batch_id IS NOT NULL;
          IF batches + linked > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0015: % import(s) are recorded and % lead(s) link to '
              'one. Export or clear them deliberately first; this migration will not '
              'discard them.', batches, linked;
          END IF;
        END $$;
        """
    )
    op.drop_index(op.f("ix_leads_import_batch_id"), table_name="leads")
    op.drop_constraint(
        op.f("fk_leads_import_batch_id_lead_import_batches"), "leads", type_="foreignkey"
    )
    op.drop_column("leads", "import_batch_id")
    op.drop_index(op.f("ix_lead_import_batches_file_sha256"), table_name="lead_import_batches")
    op.drop_table("lead_import_batches")
