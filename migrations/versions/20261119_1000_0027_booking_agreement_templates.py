"""Booking agreement templates: the app generates a one-time booking's agreement

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-19 10:00:00.000000+00:00

Owner request of 19 Nov 2026: a booking's agreement (for example a 4-hour conference room
booking) is made inside the application from an approved Word template, like a contract's.

* contract_templates.contract_type also allows BOOKING_AGREEMENT. Such a template is used
  only for bookings; contracts keep SPACE_LEASE and COWORKING_LICENCE.
* booking_documents gets template_id, template_version (both set, or both empty: a file
  uploaded by hand has neither) and data, the values the app printed.

Additive: no row changes. The downgrade refuses while a booking template or a generated
booking document exists. Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = "contract_type IN ('SPACE_LEASE', 'COWORKING_LICENCE')"
NEW_TYPES = "contract_type IN ('SPACE_LEASE', 'COWORKING_LICENCE', 'BOOKING_AGREEMENT')"


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_contract_templates_contract_type_valid"), "contract_templates", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_contract_templates_contract_type_valid"), "contract_templates", NEW_TYPES
    )
    op.add_column("booking_documents", sa.Column("template_id", sa.Uuid(), nullable=True))
    op.add_column("booking_documents", sa.Column("template_version", sa.Integer(), nullable=True))
    op.add_column(
        "booking_documents",
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_booking_documents_template_id_contract_templates"),
        "booking_documents",
        "contract_templates",
        ["template_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_booking_documents_template_with_version"),
        "booking_documents",
        "(template_id IS NULL) = (template_version IS NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM contract_templates"
            " WHERE contract_type = 'BOOKING_AGREEMENT')"
            " + (SELECT count(*) FROM booking_documents WHERE template_id IS NOT NULL)"
        )
    ).scalar()
    if used:
        raise RuntimeError(
            "Booking agreement templates or generated booking agreements exist; "
            "not downgrading below 0027."
        )
    op.drop_constraint(
        op.f("ck_booking_documents_template_with_version"), "booking_documents", type_="check"
    )
    op.drop_constraint(
        op.f("fk_booking_documents_template_id_contract_templates"),
        "booking_documents",
        type_="foreignkey",
    )
    op.drop_column("booking_documents", "data")
    op.drop_column("booking_documents", "template_version")
    op.drop_column("booking_documents", "template_id")
    op.drop_constraint(
        op.f("ck_contract_templates_contract_type_valid"), "contract_templates", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_contract_templates_contract_type_valid"), "contract_templates", OLD_TYPES
    )
