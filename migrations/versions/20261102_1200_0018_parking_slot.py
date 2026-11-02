"""Parking slot space type (BR-032, D18)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-02 12:00:00.000000+00:00

Adds PARKING_SLOT to the space types allowed by three existing checks; every existing value
stays allowed and no row is changed:

- spaces.space_type             (ck_spaces_space_type_valid)
- pricing_rules.applies_to      (ck_pricing_rules_applies_to_valid)
- leads.space_type              (ck_leads_space_type_valid)

Parking slots are ordinary rentable spaces: they are priced by the existing pricing rules
(the per-unit fixed basis, or per sq ft / per person) and held by the existing allocation
engine. No new table or column. The Parking contract category is not part of this revision.

The downgrade refuses while any space, pricing rule or lead uses PARKING_SLOT.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = (
    "'DESK', 'PRIVATE_OFFICE', 'CABIN', 'MEETING_ROOM', 'COWORKING', 'FLOOR', 'PORTION', "
    "'CONFERENCE_ROOM', 'TRAINING_ROOM', 'NON_RENTABLE', 'DEDICATED_WORKSTATION', "
    "'FLEXIBLE_WORKSTATION', 'SEAT', 'OTHER'"
)
AFTER = BEFORE + ", 'PARKING_SLOT'"

CHECKS = (
    ("spaces", "space_type", "ck_spaces_space_type_valid"),
    ("pricing_rules", "applies_to", "ck_pricing_rules_applies_to_valid"),
    ("leads", "space_type", "ck_leads_space_type_valid"),
)


def _replace(values: str) -> None:
    for table, column, name in CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, f"{column} IN ({values})")


def upgrade() -> None:
    _replace(AFTER)


def downgrade() -> None:
    # Refuse rather than leave rows the narrower checks would not allow.
    op.execute(
        """
        DO $$
        DECLARE spaces integer; rules integer; leads integer;
        BEGIN
          SELECT count(*) INTO spaces FROM spaces WHERE space_type = 'PARKING_SLOT';
          SELECT count(*) INTO rules FROM pricing_rules WHERE applies_to = 'PARKING_SLOT';
          SELECT count(*) INTO leads FROM leads WHERE space_type = 'PARKING_SLOT';
          IF spaces + rules + leads > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0018: % parking slot space(s), % pricing rule(s) and '
              '% lead(s) use PARKING_SLOT. Change or remove them deliberately first.',
              spaces, rules, leads;
          END IF;
        END $$;
        """
    )
    _replace(BEFORE)
