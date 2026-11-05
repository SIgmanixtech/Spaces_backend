"""Company profiles, contract fields and signed copies (Phase 4B, BR-012, BR-013)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-05 14:00:00.000000+00:00

Additive. Two new tables and new columns with safe defaults; no existing row, constraint
or column is changed.

- company_profiles: the legal entity that owns properties and signs their agreements
  (legal name, registered address, signatory name/designation/address, GSTIN, PAN, CIN).
  No Aadhaar.
- properties.company_profile_id (RESTRICT, indexed, nullable): several properties may
  share a profile; set from the Company Profile page.
- tenant_occupancies.proposal_id (RESTRICT, indexed, nullable): the accepted proposal a
  contract was made from. tenant_occupancies.contract_fields (JSONB, default {}): values an
  admin entered for the template's own fields.
- contract_templates.custom_fields (JSONB, default []): fields a template asks for.
- contract_signed_copies: scans of agreements signed on paper; at most one current copy
  per document (partial unique index); a replacement keeps the old copy with its reason.

The downgrade refuses while any Phase 4B data exists, like the earlier revisions.
Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["app_users.id"], name=op.f(f"fk_{table}_{column}_app_users"), ondelete="SET NULL"
    )


def upgrade() -> None:
    t = "company_profiles"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("legal_name", sa.String(length=200), nullable=False),
        sa.Column("registered_address", sa.String(length=500), nullable=True),
        sa.Column("signatory_name", sa.String(length=200), nullable=True),
        sa.Column("signatory_designation", sa.String(length=120), nullable=True),
        sa.Column("signatory_address", sa.String(length=500), nullable=True),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("pan", sa.String(length=10), nullable=True),
        sa.Column("cin", sa.String(length=21), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        _user_fk(t, "created_by"),
        _user_fk(t, "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    op.create_index(op.f(f"ix_{t}_legal_name"), t, ["legal_name"])

    op.add_column("properties", sa.Column("company_profile_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_properties_company_profile_id_company_profiles"),
        "properties",
        "company_profiles",
        ["company_profile_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_properties_company_profile_id"), "properties", ["company_profile_id"])

    t = "tenant_occupancies"
    op.add_column(t, sa.Column("proposal_id", sa.Uuid(), nullable=True))
    op.add_column(
        t,
        sa.Column(
            "contract_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        op.f(f"fk_{t}_proposal_id_proposals"),
        t,
        "proposals",
        ["proposal_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f(f"ix_{t}_proposal_id"), t, ["proposal_id"])

    op.add_column(
        "contract_templates",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )

    t = "contract_signed_copies"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=24), server_default="MANUAL_UPLOAD", nullable=False),
        sa.Column("bucket", sa.String(length=100), nullable=False),
        sa.Column("object_path", sa.String(length=500), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("signed_on", sa.Date(), nullable=False),
        sa.Column("signers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replace_reason", sa.Text(), nullable=True),
        _now("created_at"),
        sa.CheckConstraint("source IN ('MANUAL_UPLOAD')", name=op.f(f"ck_{t}_source_valid")),
        sa.CheckConstraint("size_bytes > 0", name=op.f(f"ck_{t}_size_positive")),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f(f"ck_{t}_sha256_hex")),
        sa.CheckConstraint(
            "(replaced_at IS NULL) = (replace_reason IS NULL)",
            name=op.f(f"ck_{t}_replacement_recorded"),
        ),
        sa.CheckConstraint("jsonb_array_length(signers) > 0", name=op.f(f"ck_{t}_has_signers")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["contract_documents.id"],
            name=op.f(f"fk_{t}_document_id_contract_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["tenant_occupancies.id"],
            name=op.f(f"fk_{t}_contract_id_tenant_occupancies"),
            ondelete="RESTRICT",
        ),
        _user_fk(t, "uploaded_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("object_path", name=op.f(f"uq_{t}_object_path")),
    )
    for column in ("document_id", "contract_id"):
        op.create_index(op.f(f"ix_{t}_{column}"), t, [column])
    op.create_index(
        "uq_contract_signed_copies_current",
        t,
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("replaced_at IS NULL"),
    )


def downgrade() -> None:
    # Like the earlier revisions: refuse rather than silently discard what admins entered.
    op.execute(
        """
        DO $$
        DECLARE copies integer; profiles integer; links integer; fields integer;
                custom integer;
        BEGIN
          SELECT count(*) INTO copies FROM contract_signed_copies;
          SELECT count(*) INTO profiles FROM company_profiles;
          SELECT count(*) INTO links FROM tenant_occupancies WHERE proposal_id IS NOT NULL;
          SELECT count(*) INTO fields FROM tenant_occupancies WHERE contract_fields <> '{}'::jsonb;
          SELECT count(*) INTO custom FROM contract_templates WHERE custom_fields <> '[]'::jsonb;
          IF copies + profiles + links + fields + custom > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0020: % signed cop(ies), % company profile(s), % '
              'contract(s) from proposals, % contract(s) with contract fields and % '
              'template(s) with custom fields exist. Export or clear them deliberately '
              'first; this migration will not discard them.',
              copies, profiles, links, fields, custom;
          END IF;
        END $$;
        """
    )
    op.drop_table("contract_signed_copies")
    op.drop_column("contract_templates", "custom_fields")
    op.drop_index(op.f("ix_tenant_occupancies_proposal_id"), table_name="tenant_occupancies")
    op.drop_constraint(
        op.f("fk_tenant_occupancies_proposal_id_proposals"),
        "tenant_occupancies",
        type_="foreignkey",
    )
    op.drop_column("tenant_occupancies", "contract_fields")
    op.drop_column("tenant_occupancies", "proposal_id")
    op.drop_index(op.f("ix_properties_company_profile_id"), table_name="properties")
    op.drop_constraint(
        op.f("fk_properties_company_profile_id_company_profiles"),
        "properties",
        type_="foreignkey",
    )
    op.drop_column("properties", "company_profile_id")
    op.drop_table("company_profiles")
