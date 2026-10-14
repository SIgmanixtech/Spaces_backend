-- A2 spike: the proposed allocation mechanism, reduced to what the tests exercise.
--
-- Test-only. Created inside the disposable test database in its own schema
-- (a2_spike) and dropped afterwards. Nothing here is a migration, and nothing
-- here touches the tables that revision 0001 created in "public".
--
-- Simplifications against the Phase A design: buildings are omitted (the
-- composite-key pattern for them already exists in 0001), and allocations do
-- not reference bookings or leases, which do not exist in this shape yet.

CREATE TABLE properties (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name     text NOT NULL,
  timezone text NOT NULL DEFAULT 'Asia/Kolkata'
);

CREATE TABLE floors (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id uuid NOT NULL REFERENCES properties (id) ON DELETE RESTRICT,
  name        text NOT NULL,
  CONSTRAINT uq_floors_id_property UNIQUE (id, property_id)
);

CREATE TABLE spaces (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id        uuid NOT NULL REFERENCES properties (id) ON DELETE RESTRICT,
  floor_id           uuid NULL,
  parent_space_id    uuid NULL,
  depth              smallint NOT NULL DEFAULT 0,
  parent_depth       smallint NULL,
  name               text NOT NULL,
  space_type         text NOT NULL,
  is_rentable        boolean NOT NULL DEFAULT true,
  operational_status text NOT NULL DEFAULT 'ACTIVE',
  archived_at        timestamptz NULL,
  CONSTRAINT ck_spaces_parent_pair CHECK ((parent_space_id IS NULL) = (parent_depth IS NULL)),
  CONSTRAINT ck_spaces_depth CHECK (
    depth BETWEEN 0 AND 4 AND (parent_depth IS NULL OR parent_depth = depth - 1)),
  CONSTRAINT ck_spaces_non_rentable CHECK (space_type <> 'NON_RENTABLE' OR NOT is_rentable),
  CONSTRAINT ck_spaces_hierarchy_shape CHECK (
       (floor_id IS NULL AND parent_space_id IS NULL AND depth = 0 AND space_type <> 'FLOOR')
    OR (floor_id IS NOT NULL
        AND (space_type = 'FLOOR') = (parent_space_id IS NULL)
        AND (space_type = 'FLOOR') = (depth = 0))),
  CONSTRAINT uq_spaces_id_property UNIQUE (id, property_id),
  CONSTRAINT uq_spaces_id_floor_depth UNIQUE (id, floor_id, depth),
  CONSTRAINT fk_spaces_floor FOREIGN KEY (floor_id, property_id)
    REFERENCES floors (id, property_id) ON DELETE RESTRICT,
  CONSTRAINT fk_spaces_parent FOREIGN KEY (parent_space_id, floor_id, parent_depth)
    REFERENCES spaces (id, floor_id, depth) DEFERRABLE INITIALLY DEFERRED
);
CREATE UNIQUE INDEX uq_spaces_floor_root ON spaces (floor_id) WHERE space_type = 'FLOOR';

CREATE TABLE allocations (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type     text NOT NULL,
  booking_id      uuid NULL,
  lease_space_id  uuid NULL,
  property_id     uuid NOT NULL,
  space_id        uuid NOT NULL,
  period          tstzrange NOT NULL,
  original_period tstzrange NOT NULL,
  is_active       boolean NOT NULL DEFAULT true,
  released_at     timestamptz NULL,
  release_reason  text NULL,
  CONSTRAINT ck_allocations_source CHECK (
       (source_type = 'BOOKING' AND booking_id IS NOT NULL AND lease_space_id IS NULL
        AND NOT upper_inf(period))
    OR (source_type = 'LEASE' AND lease_space_id IS NOT NULL AND booking_id IS NULL)),
  CONSTRAINT ck_allocations_period_shape CHECK (
    NOT isempty(period) AND NOT lower_inf(period)
    AND lower_inc(period) AND NOT upper_inc(period)),
  CONSTRAINT ck_allocations_release CHECK (is_active = (released_at IS NULL)),
  CONSTRAINT fk_allocations_space FOREIGN KEY (space_id, property_id)
    REFERENCES spaces (id, property_id) ON DELETE RESTRICT,
  CONSTRAINT uq_allocations_booking UNIQUE (booking_id),
  CONSTRAINT uq_allocations_lease_space UNIQUE (lease_space_id),
  CONSTRAINT uq_allocations_claim_key UNIQUE (id, period, is_active)
);

CREATE TABLE allocation_claims (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  allocation_id uuid NOT NULL,
  period        tstzrange NOT NULL,
  is_active     boolean NOT NULL,
  space_id      uuid NOT NULL REFERENCES spaces (id) ON DELETE RESTRICT,
  claim_mode    smallint NOT NULL,  -- 1 = exclusive (the target), 2 = shared (an ancestor)
  CONSTRAINT ck_claims_mode CHECK (claim_mode IN (1, 2)),
  CONSTRAINT uq_claims_allocation_space UNIQUE (allocation_id, space_id),
  CONSTRAINT fk_claims_allocation FOREIGN KEY (allocation_id, period, is_active)
    REFERENCES allocations (id, period, is_active) ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT ex_claims_exclusive EXCLUDE USING gist
    (space_id WITH =, period WITH &&) WHERE (is_active AND claim_mode = 1),
  CONSTRAINT ex_claims_mixed EXCLUDE USING gist
    (space_id WITH =, claim_mode WITH <>, period WITH &&) WHERE (is_active)
);
CREATE UNIQUE INDEX uq_claims_one_exclusive ON allocation_claims (allocation_id)
  WHERE claim_mode = 1;

-- Two transactions inserting conflicting rows under an exclusion constraint wait
-- on each other and deadlock. Locking the claimed space first, in a mode that
-- mirrors the claim, makes the second one queue instead: exclusive claims take
-- a lock that conflicts with every other claim, shared claims one that conflicts
-- only with exclusive ones. NO KEY UPDATE, not UPDATE, so rows that merely
-- reference the space (foreign-key checks) are not held up.
CREATE FUNCTION claims_lock_space() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE TRIGGER trg_claims_lock BEFORE INSERT OR UPDATE ON allocation_claims
  FOR EACH ROW EXECUTE FUNCTION claims_lock_space();

-- An active allocation that has not ended must hold exactly: an exclusive claim
-- on its space and a shared claim on every ancestor.
CREATE FUNCTION check_allocation_claim_chain(p_id uuid) RETURNS void
LANGUAGE plpgsql AS $$
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

CREATE FUNCTION trg_allocation_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM check_allocation_claim_chain(NEW.id);
  RETURN NULL;
END $$;

CREATE FUNCTION trg_claim_chain() RETURNS trigger LANGUAGE plpgsql AS $$
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
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_allocation_chain();
CREATE CONSTRAINT TRIGGER trg_claims_claim_chain
  AFTER INSERT OR UPDATE OR DELETE ON allocation_claims
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_claim_chain();

-- Allocation writers share the property row; structural space changes take it
-- exclusively. The property is the anchor because a space can never leave it.
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
  BEFORE UPDATE OF property_id, parent_space_id, floor_id, depth, space_type,
                   is_rentable, operational_status, archived_at ON spaces
  FOR EACH ROW EXECUTE FUNCTION spaces_lock_property();

-- Structural changes are refused while active or future claims touch the space.
-- A claim on the space itself already covers its subtree: every allocation of a
-- descendant holds a shared claim here.
CREATE FUNCTION spaces_guard_allocations() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  r record;
  claimed boolean;
  claimed_exclusively boolean;
BEGIN
  SELECT count(*) > 0, count(*) FILTER (WHERE claim_mode = 1) > 0
    INTO claimed, claimed_exclusively
    FROM allocation_claims
   WHERE space_id = NEW.id AND is_active AND (upper_inf(period) OR upper(period) > now());

  IF (NEW.parent_space_id, NEW.floor_id) IS DISTINCT FROM (OLD.parent_space_id, OLD.floor_id) THEN
    IF OLD.floor_id IS NULL THEN
      -- First assignment of a legacy space: allowed if its claims were rewritten.
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

  IF (claimed AND NEW.archived_at IS NOT NULL AND OLD.archived_at IS NULL)
     OR (claimed AND NEW.operational_status <> 'ACTIVE' AND OLD.operational_status = 'ACTIVE')
     OR (claimed_exclusively AND OLD.is_rentable AND NOT NEW.is_rentable) THEN
    RAISE EXCEPTION 'space % has active or future allocations', NEW.id
      USING ERRCODE = '23514', CONSTRAINT = 'ck_spaces_change_blocked';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_spaces_guard AFTER UPDATE ON spaces
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION spaces_guard_allocations();

-- Lease dates are calendar days in the property's zone, both inclusive.
CREATE FUNCTION lease_period(p_start date, p_end date, p_timezone text) RETURNS tstzrange
LANGUAGE sql STABLE AS $$
  SELECT tstzrange(
    p_start::timestamp AT TIME ZONE p_timezone,
    (p_end + 1)::timestamp AT TIME ZONE p_timezone,
    '[)')
$$;

CREATE TABLE layouts (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  floor_id       uuid NULL REFERENCES floors (id) ON DELETE RESTRICT,
  name           text NOT NULL,
  version_number integer NULL,
  status         text NOT NULL DEFAULT 'DRAFT',
  superseded_at  timestamptz NULL,
  CONSTRAINT ck_layouts_status CHECK (status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')),
  CONSTRAINT uq_layouts_floor_version UNIQUE (floor_id, version_number)
);
CREATE UNIQUE INDEX uq_layouts_one_active_per_floor ON layouts (floor_id)
  WHERE status = 'ACTIVE';
