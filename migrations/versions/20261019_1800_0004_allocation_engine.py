"""allocation engine: contracts, allocations and hierarchical claims

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19 18:00:00.000000+00:00

Brings the A2 spike's allocation design (tests/a2_spike/schema.sql) into the
application schema and makes tenant occupancies full contracts.

1. tenant_occupancies gains a contract reference, a lifecycle status, commercial
   terms and cancellation/termination metadata. Every existing row becomes an
   ACTIVE contract, which is what it already meant; its reference is assigned
   from a new sequence. The one-occupant-per-space exclusion now applies only to
   contracts that hold inventory (ACTIVE or TERMINATED).

2. allocations and allocation_claims record what holds each space. A booking or
   contract holding a space claims it EXCLUSIVELY and claims every space that
   contains it as SHARED. Two exclusion constraints then refuse, in the database,
   a space booked twice and a space held together with anything inside it, in
   either order. Siblings (5A and 5B, or two desks) only share claims and stay
   independent.

3. Triggers, as tested in the spike: a claim locks its space in a matching mode
   so concurrent writers queue rather than deadlock; allocation writers share
   the property row while structural space changes take it exclusively; every
   live allocation must hold exactly its current chain of claims; a space with
   live claims cannot be moved or archived.

4. Allocations are created for every booking and contract that holds a space
   now or in the future. If two of them already overlap across the hierarchy,
   the upgrade stops and names how many, so they can be resolved deliberately.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HOLDING = "('ACTIVE', 'TERMINATED')"


def upgrade() -> None:
    # --- 1. contracts --------------------------------------------------------------
    op.execute("CREATE SEQUENCE contract_reference_seq")
    occ = 'tenant_occupancies'
    op.add_column(occ, sa.Column(
        'reference', sa.String(length=20),
        server_default=sa.text(
            "('CT-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('contract_reference_seq')::text, 6, '0'))"),
        nullable=False))
    op.add_column(occ, sa.Column('status', sa.String(length=32),
                                 server_default='ACTIVE', nullable=False))
    op.add_column(occ, sa.Column('original_end_date', sa.Date(), nullable=True))
    op.add_column(occ, sa.Column('monthly_rent', sa.Numeric(12, 2), nullable=True))
    op.add_column(occ, sa.Column('security_deposit', sa.Numeric(12, 2), nullable=True))
    op.add_column(occ, sa.Column('currency', sa.String(length=3),
                                 server_default='INR', nullable=False))
    op.add_column(occ, sa.Column('terms', sa.Text(), nullable=True))
    op.add_column(occ, sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(occ, sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(occ, sa.Column('cancellation_reason', sa.Text(), nullable=True))
    op.add_column(occ, sa.Column('terminated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(occ, sa.Column('termination_reason', sa.Text(), nullable=True))
    op.add_column(occ, sa.Column('created_by', sa.Uuid(), nullable=True))
    # Existing occupancies took effect when they were recorded.
    op.execute("UPDATE tenant_occupancies SET activated_at = created_at")
    op.create_unique_constraint(op.f('uq_tenant_occupancies_reference'), occ, ['reference'])
    op.create_index(op.f('ix_tenant_occupancies_status'), occ, ['status'], unique=False)
    op.create_foreign_key(op.f('fk_tenant_occupancies_created_by_app_users'), occ,
                          'app_users', ['created_by'], ['id'], ondelete='SET NULL')
    op.create_check_constraint(
        op.f('ck_tenant_occupancies_status_valid'), occ,
        "status IN ('PENDING', 'ACTIVE', 'CANCELLED', 'TERMINATED')")
    op.create_check_constraint(
        op.f('ck_tenant_occupancies_amounts_not_negative'), occ,
        "(monthly_rent IS NULL OR monthly_rent >= 0)"
        " AND (security_deposit IS NULL OR security_deposit >= 0)")
    op.create_check_constraint(
        op.f('ck_tenant_occupancies_termination_recorded'), occ,
        "(status = 'TERMINATED') = (terminated_at IS NOT NULL)")
    op.create_check_constraint(
        op.f('ck_tenant_occupancies_cancellation_recorded'), occ,
        "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)")
    op.execute("ALTER TABLE tenant_occupancies DROP CONSTRAINT ex_tenant_occupancies_space_dates")
    op.execute(
        "ALTER TABLE tenant_occupancies ADD CONSTRAINT ex_tenant_occupancies_space_dates "
        "EXCLUDE USING gist (space_id WITH =, daterange(start_date, end_date, '[]') WITH &&) "
        f"WHERE (space_id IS NOT NULL AND status IN {HOLDING})")

    # --- 2. allocations and claims ---------------------------------------------------
    op.create_table(
        'allocations',
        sa.Column('source_type', sa.String(length=16), nullable=False),
        sa.Column('booking_id', sa.Uuid(), nullable=True),
        sa.Column('contract_id', sa.Uuid(), nullable=True),
        sa.Column('property_id', sa.Uuid(), nullable=False),
        sa.Column('space_id', sa.Uuid(), nullable=False),
        sa.Column('period', postgresql.TSTZRANGE(), nullable=False),
        sa.Column('original_period', postgresql.TSTZRANGE(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('release_reason', sa.Text(), nullable=True),
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.CheckConstraint("source_type IN ('BOOKING', 'CONTRACT')",
                           name=op.f('ck_allocations_source_type_valid')),
        sa.CheckConstraint(
            "(source_type = 'BOOKING' AND booking_id IS NOT NULL AND contract_id IS NULL"
            " AND NOT upper_inf(period))"
            " OR (source_type = 'CONTRACT' AND contract_id IS NOT NULL AND booking_id IS NULL)",
            name=op.f('ck_allocations_source')),
        sa.CheckConstraint(
            "NOT isempty(period) AND NOT lower_inf(period)"
            " AND lower_inc(period) AND NOT upper_inc(period)",
            name=op.f('ck_allocations_period_shape')),
        sa.CheckConstraint("is_active = (released_at IS NULL)", name=op.f('ck_allocations_release')),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'],
                                name=op.f('fk_allocations_booking_id_bookings'), ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['contract_id'], ['tenant_occupancies.id'],
                                name=op.f('fk_allocations_contract_id_tenant_occupancies'),
                                ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['space_id', 'property_id'], ['spaces.id', 'spaces.property_id'],
                                name=op.f('fk_allocations_space_id_spaces'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_allocations')),
        sa.UniqueConstraint('booking_id', name='uq_allocations_booking'),
        sa.UniqueConstraint('contract_id', name='uq_allocations_contract'),
        sa.UniqueConstraint('id', 'period', 'is_active', name='uq_allocations_claim_key'),
    )
    op.create_index(op.f('ix_allocations_property_id'), 'allocations', ['property_id'])
    op.create_index(op.f('ix_allocations_space_id'), 'allocations', ['space_id'])

    op.create_table(
        'allocation_claims',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('allocation_id', sa.Uuid(), nullable=False),
        sa.Column('period', postgresql.TSTZRANGE(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('space_id', sa.Uuid(), nullable=False),
        sa.Column('claim_mode', sa.SmallInteger(), nullable=False),
        sa.CheckConstraint('claim_mode IN (1, 2)', name=op.f('ck_allocation_claims_mode')),
        sa.ForeignKeyConstraint(['allocation_id', 'period', 'is_active'],
                                ['allocations.id', 'allocations.period', 'allocations.is_active'],
                                name='fk_allocation_claims_allocation',
                                onupdate='CASCADE', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['space_id'], ['spaces.id'],
                                name=op.f('fk_allocation_claims_space_id_spaces'),
                                ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_allocation_claims')),
        sa.UniqueConstraint('allocation_id', 'space_id',
                            name='uq_allocation_claims_allocation_space'),
    )
    op.create_index(op.f('ix_allocation_claims_space_id'), 'allocation_claims', ['space_id'])
    op.create_index('uq_allocation_claims_one_exclusive', 'allocation_claims', ['allocation_id'],
                    unique=True, postgresql_where=sa.text('claim_mode = 1'))
    op.execute(
        "ALTER TABLE allocation_claims ADD CONSTRAINT ex_allocation_claims_exclusive "
        "EXCLUDE USING gist (space_id WITH =, period WITH &&) WHERE (is_active AND claim_mode = 1)")
    op.execute(
        "ALTER TABLE allocation_claims ADD CONSTRAINT ex_allocation_claims_mixed "
        "EXCLUDE USING gist (space_id WITH =, claim_mode WITH <>, period WITH &&) "
        "WHERE (is_active)")

    # --- 3. triggers -----------------------------------------------------------------
    # Two transactions inserting conflicting rows under an exclusion constraint can
    # deadlock. Locking the claimed space first, in a mode mirroring the claim, makes
    # the second queue instead. Claims are written top-down (floor, portion, space),
    # so every writer takes these locks in the same order.
    op.execute("""
CREATE FUNCTION allocation_claims_lock_space() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT NEW.is_active THEN
    RETURN NEW;
  END IF;
  IF NEW.claim_mode = 1 THEN
    PERFORM 1 FROM spaces WHERE id = NEW.space_id FOR NO KEY UPDATE;
  ELSE
    PERFORM 1 FROM spaces WHERE id = NEW.space_id FOR SHARE;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_allocation_claims_lock BEFORE INSERT OR UPDATE ON allocation_claims
  FOR EACH ROW EXECUTE FUNCTION allocation_claims_lock_space();
""")
    # A live allocation (active, not yet ended) holds exactly: an exclusive claim on
    # its space and a shared claim on every space containing it.
    op.execute("""
CREATE FUNCTION check_allocation_claim_chain(p_id uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  a allocations%ROWTYPE;
  bad integer;
BEGIN
  SELECT * INTO a FROM allocations WHERE id = p_id;
  IF NOT FOUND OR NOT a.is_active
     OR (NOT upper_inf(a.period) AND upper(a.period) <= now()) THEN
    RETURN;
  END IF;
  WITH RECURSIVE chain AS (
      SELECT s.id, s.parent_space_id, 1::smallint AS claim_mode
        FROM spaces s WHERE s.id = a.space_id
      UNION ALL
      SELECT p.id, p.parent_space_id, 2::smallint
        FROM spaces p JOIN chain c ON p.id = c.parent_space_id),
  expected AS (SELECT id AS space_id, claim_mode FROM chain),
  actual AS (SELECT space_id, claim_mode FROM allocation_claims WHERE allocation_id = p_id)
  SELECT count(*) INTO bad FROM (
      (SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL
      (SELECT * FROM actual EXCEPT SELECT * FROM expected)) d;
  IF bad > 0 THEN
    RAISE EXCEPTION 'allocation % has an incomplete or stale claim chain', p_id
      USING ERRCODE = '23514', CONSTRAINT = 'ck_allocation_claim_chain';
  END IF;
END $$;
CREATE FUNCTION allocations_check_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM check_allocation_claim_chain(NEW.id);
  RETURN NULL;
END $$;
CREATE FUNCTION allocation_claims_check_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM check_allocation_claim_chain(OLD.allocation_id);
  ELSE
    PERFORM check_allocation_claim_chain(NEW.allocation_id);
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_allocations_claim_chain
  AFTER INSERT OR UPDATE ON allocations
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION allocations_check_chain();
CREATE CONSTRAINT TRIGGER trg_allocation_claims_claim_chain
  AFTER INSERT OR UPDATE OR DELETE ON allocation_claims
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION allocation_claims_check_chain();
""")
    # Allocation writers share the property row; structural space changes take it
    # exclusively, so the hierarchy cannot change under a writer's feet.
    op.execute("""
CREATE FUNCTION allocations_lock_property() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'allocation writes require READ COMMITTED' USING ERRCODE = '25001';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.space_id, NEW.property_id)
                          IS DISTINCT FROM (OLD.space_id, OLD.property_id) THEN
    RAISE EXCEPTION 'an allocation cannot change its space'
      USING ERRCODE = '23514', CONSTRAINT = 'ck_allocations_space_immutable';
  END IF;
  PERFORM 1 FROM properties WHERE id = NEW.property_id FOR KEY SHARE;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_allocations_lock BEFORE INSERT OR UPDATE ON allocations
  FOR EACH ROW EXECUTE FUNCTION allocations_lock_property();

CREATE FUNCTION spaces_lock_property() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.property_id IS DISTINCT FROM OLD.property_id THEN
    RAISE EXCEPTION 'a space cannot change property'
      USING ERRCODE = '23514', CONSTRAINT = 'ck_spaces_property_immutable';
  END IF;
  PERFORM 1 FROM properties WHERE id = NEW.property_id FOR UPDATE;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_spaces_lock
  BEFORE UPDATE OF property_id, parent_space_id, floor_id, depth, archived_at ON spaces
  FOR EACH ROW EXECUTE FUNCTION spaces_lock_property();
""")
    # A space with live claims cannot be moved or archived. A claim on the space
    # itself covers its subtree: every allocation inside it holds a shared claim here.
    # Assigning a space that was never on a floor is allowed once its claims are
    # rewritten for the new hierarchy (the chain check verifies that at commit).
    op.execute("""
CREATE FUNCTION spaces_guard_allocations() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  r record;
  claimed boolean;
BEGIN
  SELECT count(*) > 0 INTO claimed
    FROM allocation_claims
   WHERE space_id = NEW.id AND is_active AND (upper_inf(period) OR upper(period) > now());

  IF (NEW.parent_space_id, NEW.floor_id) IS DISTINCT FROM (OLD.parent_space_id, OLD.floor_id) THEN
    IF OLD.floor_id IS NULL THEN
      FOR r IN SELECT DISTINCT allocation_id FROM allocation_claims
                WHERE space_id = NEW.id AND is_active
                  AND (upper_inf(period) OR upper(period) > now()) LOOP
        PERFORM check_allocation_claim_chain(r.allocation_id);
      END LOOP;
    ELSIF claimed THEN
      RAISE EXCEPTION 'space % has active or future allocations', NEW.id
        USING ERRCODE = '23514', CONSTRAINT = 'ck_spaces_move_blocked';
    END IF;
  END IF;

  IF claimed AND NEW.archived_at IS NOT NULL AND OLD.archived_at IS NULL THEN
    RAISE EXCEPTION 'space % has active or future allocations', NEW.id
      USING ERRCODE = '23514', CONSTRAINT = 'ck_spaces_change_blocked';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_spaces_guard AFTER UPDATE ON spaces
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION spaces_guard_allocations();
""")

    # --- 4. allocations for what holds a space now or later -------------------------------
    # Contract dates are calendar days in the business time zone, as everywhere else.
    from app.core.config import get_settings

    tz = get_settings().business_timezone.replace("'", "''")
    op.execute(f"""
CREATE TEMPORARY TABLE pending_allocations ON COMMIT DROP AS
SELECT gen_random_uuid() AS id, 'BOOKING'::text AS source_type, b.id AS booking_id,
       NULL::uuid AS contract_id, b.property_id, b.space_id,
       tstzrange(b.start_at, b.end_at, '[)') AS period
  FROM bookings b
 WHERE b.status <> 'CANCELLED' AND b.end_at > now()
UNION ALL
SELECT gen_random_uuid(), 'CONTRACT', NULL, o.id, o.property_id, o.space_id,
       tstzrange(o.start_date::timestamp AT TIME ZONE '{tz}',
                 CASE WHEN o.end_date IS NULL THEN NULL
                      ELSE (o.end_date + 1)::timestamp AT TIME ZONE '{tz}' END,
                 '[)')
  FROM tenant_occupancies o
 WHERE o.space_id IS NOT NULL AND o.status IN {HOLDING}
   AND (o.end_date IS NULL
        OR (o.end_date + 1)::timestamp AT TIME ZONE '{tz}' > now());

CREATE TEMPORARY TABLE pending_claims ON COMMIT DROP AS
WITH RECURSIVE chain AS (
    SELECT a.id AS allocation_id, a.period, s.id AS space_id, s.parent_space_id,
           1::smallint AS claim_mode, 0 AS level
      FROM pending_allocations a JOIN spaces s ON s.id = a.space_id
    UNION ALL
    SELECT c.allocation_id, c.period, p.id, p.parent_space_id, 2::smallint, c.level + 1
      FROM chain c JOIN spaces p ON p.id = c.parent_space_id)
SELECT allocation_id, period, space_id, claim_mode, level FROM chain;

DO $$
DECLARE clashes integer;
BEGIN
  SELECT count(*) INTO clashes
    FROM pending_claims x JOIN pending_claims y
      ON x.space_id = y.space_id AND x.allocation_id < y.allocation_id
     AND x.period && y.period AND (x.claim_mode = 1 OR y.claim_mode = 1);
  IF clashes > 0 THEN
    RAISE EXCEPTION
      'Cannot upgrade to 0004: % pair(s) of current or future bookings/contracts overlap on '
      'the same space or across a space and something inside it. Cancel, move or reschedule '
      'them first; this migration will not choose which one wins.', clashes;
  END IF;
END $$;

INSERT INTO allocations (id, source_type, booking_id, contract_id, property_id, space_id,
                         period, original_period)
SELECT id, source_type, booking_id, contract_id, property_id, space_id, period, period
  FROM pending_allocations;
INSERT INTO allocation_claims (allocation_id, period, is_active, space_id, claim_mode)
SELECT allocation_id, period, true, space_id, claim_mode
  FROM pending_claims ORDER BY allocation_id, level DESC;
""")


def downgrade() -> None:
    op.execute(f"""
DO $$
DECLARE used integer;
BEGIN
  SELECT count(*) INTO used FROM tenant_occupancies
   WHERE status <> 'ACTIVE' OR original_end_date IS NOT NULL OR monthly_rent IS NOT NULL
      OR security_deposit IS NOT NULL OR terms IS NOT NULL OR currency <> 'INR'
      OR cancellation_reason IS NOT NULL OR termination_reason IS NOT NULL;
  IF used > 0 THEN
    RAISE EXCEPTION
      'Cannot downgrade past 0004: % contract(s) are pending, cancelled or terminated, or '
      'carry commercial terms. Revision 0003 would read them as plain occupancies. Export or '
      'clear that data deliberately first; this migration will not discard it.', used;
  END IF;
END $$;
""")
    # Allocations are derived from bookings and contracts and are rebuilt on upgrade.
    op.execute("""
DROP TRIGGER IF EXISTS trg_spaces_guard ON spaces;
DROP TRIGGER IF EXISTS trg_spaces_lock ON spaces;
DROP FUNCTION IF EXISTS spaces_guard_allocations();
DROP FUNCTION IF EXISTS spaces_lock_property();
""")
    op.drop_table('allocation_claims')
    op.drop_table('allocations')
    op.execute("""
DROP FUNCTION IF EXISTS allocation_claims_lock_space();
DROP FUNCTION IF EXISTS allocations_check_chain();
DROP FUNCTION IF EXISTS allocation_claims_check_chain();
DROP FUNCTION IF EXISTS check_allocation_claim_chain(uuid);
DROP FUNCTION IF EXISTS allocations_lock_property();
""")

    occ = 'tenant_occupancies'
    op.execute("ALTER TABLE tenant_occupancies DROP CONSTRAINT ex_tenant_occupancies_space_dates")
    op.execute(
        "ALTER TABLE tenant_occupancies ADD CONSTRAINT ex_tenant_occupancies_space_dates "
        "EXCLUDE USING gist (space_id WITH =, daterange(start_date, end_date, '[]') WITH &&) "
        "WHERE (space_id IS NOT NULL)")
    for name in ('cancellation_recorded', 'termination_recorded', 'amounts_not_negative',
                 'status_valid'):
        op.drop_constraint(op.f(f'ck_tenant_occupancies_{name}'), occ, type_='check')
    op.drop_constraint(op.f('fk_tenant_occupancies_created_by_app_users'), occ,
                       type_='foreignkey')
    op.drop_index(op.f('ix_tenant_occupancies_status'), table_name=occ)
    op.drop_constraint(op.f('uq_tenant_occupancies_reference'), occ, type_='unique')
    for column in ('created_by', 'termination_reason', 'terminated_at', 'cancellation_reason',
                   'cancelled_at', 'activated_at', 'terms', 'currency', 'security_deposit',
                   'monthly_rent', 'original_end_date', 'status', 'reference'):
        op.drop_column(occ, column)
    op.execute("DROP SEQUENCE contract_reference_seq")
