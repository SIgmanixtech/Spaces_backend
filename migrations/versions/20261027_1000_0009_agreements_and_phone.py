"""contract agreements (templates and generated documents) and mobile sign-in

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27 10:00:00.000000+00:00

Additive. Nothing existing changes meaning and no row is written.

1. contract_templates: admin-uploaded Word templates, one contract type each
   (SPACE_LEASE or COWORKING_LICENCE), versioned, DRAFT until approved, at most one
   ACTIVE per type for a property or as the default.
2. contract_documents: documents generated from a template for a contract, keeping
   the template and version used; GENERATED -> APPROVED -> SENT_FOR_SIGNATURE ->
   SIGNED only by explicit administrator actions.
3. tenant_occupancies.contract_type and customer_details; spaces.contract_type.
4. app_users.phone (unique, E.164) for customers who sign in with a mobile number;
   app_users.email becomes optional, and every user must have an email or a phone.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPES = "contract_type IN ('SPACE_LEASE', 'COWORKING_LICENCE')"
NO_PROPERTY = "00000000-0000-0000-0000-000000000000"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        'contract_templates',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('contract_type', sa.String(length=32), nullable=False),
        sa.Column('property_id', sa.Uuid(), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='DRAFT', nullable=False),
        sa.Column('bucket', sa.String(length=100), nullable=False),
        sa.Column('object_path', sa.String(length=500), nullable=False),
        sa.Column('original_name', sa.String(length=255), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column(
            'placeholders', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column(
            'required_fields', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('uploaded_by', sa.Uuid(), nullable=True),
        sa.Column('approved_by', sa.Uuid(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(TYPES, name=op.f('ck_contract_templates_contract_type_valid')),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'RETIRED')",
            name=op.f('ck_contract_templates_status_valid'),
        ),
        sa.CheckConstraint('version > 0', name=op.f('ck_contract_templates_version_positive')),
        sa.CheckConstraint(
            "(status = 'DRAFT') = (approved_at IS NULL)",
            name=op.f('ck_contract_templates_approved_unless_draft'),
        ),
        sa.ForeignKeyConstraint(
            ['property_id'], ['properties.id'],
            name=op.f('fk_contract_templates_property_id_properties'), ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['uploaded_by'], ['app_users.id'],
            name=op.f('fk_contract_templates_uploaded_by_app_users'), ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['approved_by'], ['app_users.id'],
            name=op.f('fk_contract_templates_approved_by_app_users'), ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_templates')),
        sa.UniqueConstraint(
            'contract_type', 'property_id', 'version', name='uq_contract_templates_version'
        ),
        sa.UniqueConstraint('object_path', name=op.f('uq_contract_templates_object_path')),
    )
    op.create_index(
        op.f('ix_contract_templates_property_id'), 'contract_templates', ['property_id'],
        unique=False,
    )
    op.create_index(
        'uq_contract_templates_active', 'contract_templates',
        ['contract_type', sa.text(f"coalesce(property_id, '{NO_PROPERTY}'::uuid)")],
        unique=True, postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        'contract_documents',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('contract_id', sa.Uuid(), nullable=False),
        sa.Column('template_id', sa.Uuid(), nullable=False),
        sa.Column('template_version', sa.Integer(), nullable=False),
        sa.Column('contract_type', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=24), server_default='GENERATED', nullable=False),
        sa.Column('bucket', sa.String(length=100), nullable=False),
        sa.Column('object_path', sa.String(length=500), nullable=False),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('input_hash', sa.String(length=64), nullable=False),
        sa.Column(
            'data', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column('generated_by', sa.Uuid(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('GENERATED', 'APPROVED', 'SENT_FOR_SIGNATURE', 'SIGNED', 'SUPERSEDED')",
            name=op.f('ck_contract_documents_status_valid'),
        ),
        sa.CheckConstraint(TYPES, name=op.f('ck_contract_documents_contract_type_valid')),
        sa.ForeignKeyConstraint(
            ['contract_id'], ['tenant_occupancies.id'],
            name=op.f('fk_contract_documents_contract_id_tenant_occupancies'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['template_id'], ['contract_templates.id'],
            name=op.f('fk_contract_documents_template_id_contract_templates'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['generated_by'], ['app_users.id'],
            name=op.f('fk_contract_documents_generated_by_app_users'), ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_documents')),
        sa.UniqueConstraint('contract_id', 'input_hash', name='uq_contract_documents_inputs'),
        sa.UniqueConstraint('object_path', name=op.f('uq_contract_documents_object_path')),
    )
    op.create_index(
        op.f('ix_contract_documents_contract_id'), 'contract_documents', ['contract_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_contract_documents_template_id'), 'contract_documents', ['template_id'],
        unique=False,
    )

    occ = 'tenant_occupancies'
    op.add_column(occ, sa.Column('contract_type', sa.String(length=32), nullable=True))
    op.add_column(
        occ,
        sa.Column(
            'customer_details', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )
    op.create_check_constraint(op.f('ck_tenant_occupancies_contract_type_valid'), occ, TYPES)
    op.add_column('spaces', sa.Column('contract_type', sa.String(length=32), nullable=True))
    op.create_check_constraint(op.f('ck_spaces_contract_type_valid'), 'spaces', TYPES)

    op.add_column('app_users', sa.Column('phone', sa.String(length=20), nullable=True))
    op.create_unique_constraint(op.f('uq_app_users_phone'), 'app_users', ['phone'])
    op.alter_column('app_users', 'email', existing_type=sa.String(length=320), nullable=True)
    op.create_check_constraint(
        op.f('ck_app_users_has_contact'), 'app_users', 'email IS NOT NULL OR phone IS NOT NULL'
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE templates integer; documents integer; phone_only integer; typed integer;
        BEGIN
          SELECT count(*) INTO templates FROM contract_templates;
          SELECT count(*) INTO documents FROM contract_documents;
          SELECT count(*) INTO phone_only FROM app_users WHERE email IS NULL;
          SELECT (SELECT count(*) FROM tenant_occupancies
                   WHERE contract_type IS NOT NULL OR customer_details <> '{}'::jsonb)
               + (SELECT count(*) FROM spaces WHERE contract_type IS NOT NULL) INTO typed;
          IF templates + documents + phone_only + typed > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0009: % template(s), % generated document(s), '
              '% user(s) without an email and % contract/space setting(s) exist. Export or '
              'clear them deliberately first; this migration will not discard them.',
              templates, documents, phone_only, typed;
          END IF;
        END $$;
        """
    )
    op.drop_constraint(op.f('ck_app_users_has_contact'), 'app_users', type_='check')
    op.alter_column('app_users', 'email', existing_type=sa.String(length=320), nullable=False)
    op.drop_constraint(op.f('uq_app_users_phone'), 'app_users', type_='unique')
    op.drop_column('app_users', 'phone')
    op.drop_constraint(op.f('ck_spaces_contract_type_valid'), 'spaces', type_='check')
    op.drop_column('spaces', 'contract_type')
    occ = 'tenant_occupancies'
    op.drop_constraint(op.f('ck_tenant_occupancies_contract_type_valid'), occ, type_='check')
    op.drop_column(occ, 'customer_details')
    op.drop_column(occ, 'contract_type')
    op.drop_index(op.f('ix_contract_documents_template_id'), table_name='contract_documents')
    op.drop_index(op.f('ix_contract_documents_contract_id'), table_name='contract_documents')
    op.drop_table('contract_documents')
    op.drop_index('uq_contract_templates_active', table_name='contract_templates')
    op.drop_index(op.f('ix_contract_templates_property_id'), table_name='contract_templates')
    op.drop_table('contract_templates')
