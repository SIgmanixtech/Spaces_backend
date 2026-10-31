"""Amenities at building, floor and space level (BR-031, D16 option C)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-30 18:00:00.000000+00:00

Additive. The existing property-amenity link gains optional building_id, floor_id and
space_id; every existing row stays a property-level assignment, unchanged.

- Composite foreign keys make the building, floor and space belong to the row's own
  property (the pattern spaces already use). RESTRICT, like the rest of the hierarchy.
- level_shape: property = no ids; building = building_id; floor = floor_id with its
  building_id; space = space_id alone.
- The (property, amenity) unique constraint becomes four partial unique indexes, one per
  level, so the same amenity may be offered at a property and on one of its floors.
  The property-level one keeps the old name, so its error message is unchanged.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "property_amenities"


def upgrade() -> None:
    for column in ("building_id", "floor_id", "space_id"):
        op.add_column(T, sa.Column(column, sa.Uuid(), nullable=True))
        op.create_index(op.f(f"ix_{T}_{column}"), T, [column])
    op.create_foreign_key(
        op.f(f"fk_{T}_building_id_buildings"),
        T,
        "buildings",
        ["building_id", "property_id"],
        ["id", "property_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f(f"fk_{T}_floor_id_floors"),
        T,
        "floors",
        ["floor_id", "building_id"],
        ["id", "building_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f(f"fk_{T}_space_id_spaces"),
        T,
        "spaces",
        ["space_id", "property_id"],
        ["id", "property_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f(f"ck_{T}_level_shape"),
        T,
        "(space_id IS NULL OR (building_id IS NULL AND floor_id IS NULL))"
        " AND (floor_id IS NULL OR building_id IS NOT NULL)",
    )
    op.drop_constraint("uq_property_amenities_pair", T, type_="unique")
    op.create_index(
        "uq_property_amenities_pair",
        T,
        ["property_id", "amenity_id"],
        unique=True,
        postgresql_where=sa.text("building_id IS NULL AND space_id IS NULL"),
    )
    op.create_index(
        "uq_property_amenities_building",
        T,
        ["building_id", "amenity_id"],
        unique=True,
        postgresql_where=sa.text("building_id IS NOT NULL AND floor_id IS NULL"),
    )
    op.create_index(
        "uq_property_amenities_floor",
        T,
        ["floor_id", "amenity_id"],
        unique=True,
        postgresql_where=sa.text("floor_id IS NOT NULL"),
    )
    op.create_index(
        "uq_property_amenities_space",
        T,
        ["space_id", "amenity_id"],
        unique=True,
        postgresql_where=sa.text("space_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Like 0002-0009 and 0012: refuse rather than silently discard what admins entered.
    op.execute(
        """
        DO $$
        DECLARE placed integer;
        BEGIN
          SELECT count(*) INTO placed FROM property_amenities
            WHERE building_id IS NOT NULL OR space_id IS NOT NULL;
          IF placed > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0013: % building, floor or space amenity assignment(s) '
              'exist. Export or remove them deliberately first; this migration will not '
              'discard them.', placed;
          END IF;
        END $$;
        """
    )
    for name in (
        "uq_property_amenities_space",
        "uq_property_amenities_floor",
        "uq_property_amenities_building",
        "uq_property_amenities_pair",
    ):
        op.drop_index(name, table_name=T)
    # Only property-level rows remain, and they were unique per (property, amenity).
    op.create_unique_constraint("uq_property_amenities_pair", T, ["property_id", "amenity_id"])
    op.drop_constraint(op.f(f"ck_{T}_level_shape"), T, type_="check")
    for column, target in (
        ("space_id", "spaces"),
        ("floor_id", "floors"),
        ("building_id", "buildings"),
    ):
        op.drop_constraint(op.f(f"fk_{T}_{column}_{target}"), T, type_="foreignkey")
    for column in ("space_id", "floor_id", "building_id"):
        op.drop_index(op.f(f"ix_{T}_{column}"), table_name=T)
        op.drop_column(T, column)
