"""The two exclusion constraints and the cascading key, on one connection."""

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from tests.a2_spike.helpers import (
    EXCLUSIVE,
    SHARED,
    add_space,
    allocate,
    build_inventory,
    claim_chain,
    hours,
    insert_allocation,
    outcome,
)

MIXED = "23P01:ex_claims_mixed"
EXCLUSIVE_CONFLICT = "23P01:ex_claims_exclusive"


def attempt(conn: Connection, *args: object, **kwargs: object) -> str:
    """Allocate inside a savepoint so a rejected attempt leaves the transaction usable."""

    def action() -> None:
        with conn.begin_nested():
            allocate(conn, *args, **kwargs)

    return outcome(action)


def test_chain_is_exclusive_on_the_target_and_shared_on_ancestors(conn: Connection) -> None:
    inv = build_inventory(conn)
    assert claim_chain(conn, inv.d1) == [
        (inv.root, SHARED),
        (inv.p1, SHARED),
        (inv.d1, EXCLUSIVE),
    ]
    assert claim_chain(conn, inv.root) == [(inv.root, EXCLUSIVE)]


def test_sibling_desks_can_be_allocated_for_the_same_time(conn: Connection) -> None:
    inv = build_inventory(conn)
    assert attempt(conn, inv.property_id, inv.d1, *hours(9, 10)) == "ok"
    assert attempt(conn, inv.property_id, inv.d2, *hours(9, 10)) == "ok"
    assert attempt(conn, inv.property_id, inv.d3, *hours(9, 10)) == "ok"
    conn.commit()


def test_same_space_cannot_be_allocated_twice(conn: Connection) -> None:
    inv = build_inventory(conn)
    assert attempt(conn, inv.property_id, inv.d1, *hours(9, 11)) == "ok"
    assert attempt(conn, inv.property_id, inv.d1, *hours(10, 12)) == EXCLUSIVE_CONFLICT


@pytest.mark.parametrize("floor_first", [True, False])
def test_whole_floor_conflicts_with_a_desk_in_either_order(
    conn: Connection, floor_first: bool
) -> None:
    inv = build_inventory(conn)
    first, second = (inv.root, inv.d1) if floor_first else (inv.d1, inv.root)
    assert attempt(conn, inv.property_id, first, *hours(9, 11)) == "ok"
    assert attempt(conn, inv.property_id, second, *hours(10, 12)) == MIXED


@pytest.mark.parametrize("portion_first", [True, False])
def test_portion_conflicts_with_a_desk_inside_it_in_either_order(
    conn: Connection, portion_first: bool
) -> None:
    inv = build_inventory(conn)
    first, second = (inv.p1, inv.d2) if portion_first else (inv.d2, inv.p1)
    assert attempt(conn, inv.property_id, first, *hours(9, 11)) == "ok"
    assert attempt(conn, inv.property_id, second, *hours(10, 12)) == MIXED


@pytest.mark.parametrize("floor_first", [True, False])
def test_whole_floor_conflicts_with_a_portion_in_either_order(
    conn: Connection, floor_first: bool
) -> None:
    inv = build_inventory(conn)
    first, second = (inv.root, inv.p2) if floor_first else (inv.p2, inv.root)
    assert attempt(conn, inv.property_id, first, *hours(9, 11)) == "ok"
    assert attempt(conn, inv.property_id, second, *hours(10, 12)) == MIXED


def test_independent_branches_do_not_conflict(conn: Connection) -> None:
    inv = build_inventory(conn)
    assert attempt(conn, inv.property_id, inv.p1, *hours(9, 11)) == "ok"
    assert attempt(conn, inv.property_id, inv.p2, *hours(9, 11)) == "ok"
    conn.rollback()
    assert attempt(conn, inv.property_id, inv.p1, *hours(9, 11)) == "ok"
    # A desk in the other portion is unaffected by the first portion's allocation.
    assert attempt(conn, inv.property_id, inv.d3, *hours(9, 11)) == "ok"


def test_bookings_and_leases_share_one_conflict_domain(conn: Connection) -> None:
    inv = build_inventory(conn)
    assert attempt(conn, inv.property_id, inv.p1, *hours(0, 24 * 30), source="LEASE") == "ok"
    assert attempt(conn, inv.property_id, inv.d1, *hours(9, 10), source="BOOKING") == MIXED
    assert attempt(conn, inv.property_id, inv.d3, *hours(9, 10), source="BOOKING") == "ok"
    assert attempt(conn, inv.property_id, inv.root, *hours(9, 10), source="LEASE") == MIXED


def test_adjacent_half_open_periods_do_not_conflict(conn: Connection) -> None:
    inv = build_inventory(conn)
    assert attempt(conn, inv.property_id, inv.d1, *hours(9, 10)) == "ok"
    assert attempt(conn, inv.property_id, inv.d1, *hours(10, 11)) == "ok"
    assert attempt(conn, inv.property_id, inv.root, *hours(11, 12)) == "ok"


def test_open_ended_lease_blocks_everything_after_its_start(conn: Connection) -> None:
    inv = build_inventory(conn)
    start, _ = hours(48, 49)
    assert attempt(conn, inv.property_id, inv.p1, start, None, source="LEASE") == "ok"
    assert attempt(conn, inv.property_id, inv.d1, *hours(9, 10)) == "ok"  # before the lease
    assert attempt(conn, inv.property_id, inv.d1, *hours(24 * 400, 24 * 400 + 1)) == MIXED


def test_child_added_after_the_floor_is_leased_cannot_be_allocated(conn: Connection) -> None:
    inv = build_inventory(conn)
    assert attempt(conn, inv.property_id, inv.root, *hours(0, 24), source="LEASE") == "ok"
    new_desk = add_space(conn, inv.property_id, inv.floor_id, inv.p2, "DESK", "D-new")
    assert attempt(conn, inv.property_id, new_desk, *hours(9, 10)) == MIXED


def test_unassigned_legacy_space_is_an_isolated_node(conn: Connection) -> None:
    inv = build_inventory(conn)
    legacy = add_space(conn, inv.property_id, None, None, "MEETING_ROOM", "Legacy room")
    assert claim_chain(conn, legacy) == [(legacy, EXCLUSIVE)]
    assert attempt(conn, inv.property_id, inv.root, *hours(9, 11), source="LEASE") == "ok"
    # Not under the floor, so the floor lease does not block it; double booking still does.
    assert attempt(conn, inv.property_id, legacy, *hours(9, 11)) == "ok"
    assert attempt(conn, inv.property_id, legacy, *hours(10, 12)) == EXCLUSIVE_CONFLICT


def test_releasing_frees_the_period_and_keeps_the_rows(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocation_id = allocate(conn, inv.property_id, inv.root, *hours(9, 11), source="LEASE")
    conn.execute(
        text(
            "UPDATE allocations SET is_active = false, released_at = now(),"
            " release_reason = 'cancelled' WHERE id = :id"
        ),
        {"id": allocation_id},
    )
    assert attempt(conn, inv.property_id, inv.d1, *hours(9, 11)) == "ok"
    conn.commit()
    rows = conn.execute(
        text("SELECT is_active FROM allocation_claims WHERE allocation_id = :id"),
        {"id": allocation_id},
    ).all()
    assert [row.is_active for row in rows] == [False]


def test_reschedule_cascades_to_every_claim(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocation_id = allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    start, end = hours(13, 14)
    conn.execute(
        text("UPDATE allocations SET period = tstzrange(:s, :e, '[)') WHERE id = :id"),
        {"s": start, "e": end, "id": allocation_id},
    )
    conn.commit()
    periods = conn.execute(
        text(
            "SELECT DISTINCT lower(period) AS s, upper(period) AS e"
            " FROM allocation_claims WHERE allocation_id = :id"
        ),
        {"id": allocation_id},
    ).all()
    assert [(row.s, row.e) for row in periods] == [(start, end)]
    assert (
        conn.execute(
            text("SELECT count(*) FROM allocation_claims WHERE allocation_id = :id"),
            {"id": allocation_id},
        ).scalar_one()
        == 3
    )


def test_reschedule_into_a_conflict_is_rejected_and_changes_nothing(conn: Connection) -> None:
    inv = build_inventory(conn)
    desk = allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    allocate(conn, inv.property_id, inv.p1, *hours(13, 15), source="LEASE")
    conn.commit()

    start, end = hours(14, 16)
    with pytest.raises(IntegrityError) as raised:
        conn.execute(
            text("UPDATE allocations SET period = tstzrange(:s, :e, '[)') WHERE id = :id"),
            {"s": start, "e": end, "id": desk},
        )
    # The conflict is found on a claim row, reached through the cascade.
    assert raised.value.orig.sqlstate == "23P01"
    assert raised.value.orig.diag.constraint_name == "ex_claims_mixed"
    assert raised.value.orig.diag.table_name == "allocation_claims"
    conn.rollback()

    original = hours(9, 10)
    assert (
        conn.execute(
            text("SELECT lower(period), upper(period) FROM allocations WHERE id = :id"),
            {"id": desk},
        ).one()
        == original
    )
    assert conn.execute(
        text(
            "SELECT DISTINCT lower(period), upper(period) FROM allocation_claims"
            " WHERE allocation_id = :id"
        ),
        {"id": desk},
    ).all() == [original]


def test_truncating_a_lease_frees_the_remainder(conn: Connection) -> None:
    inv = build_inventory(conn)
    lease = allocate(conn, inv.property_id, inv.p1, *hours(0, 100), source="LEASE")
    assert attempt(conn, inv.property_id, inv.d1, *hours(60, 61)) == MIXED
    start, end = hours(0, 50)
    conn.execute(
        text("UPDATE allocations SET period = tstzrange(:s, :e, '[)') WHERE id = :id"),
        {"s": start, "e": end, "id": lease},
    )
    assert attempt(conn, inv.property_id, inv.d1, *hours(60, 61)) == "ok"
    assert attempt(conn, inv.property_id, inv.d1, *hours(49, 51)) == MIXED
    conn.commit()
    assert (
        conn.execute(
            text("SELECT upper(original_period) FROM allocations WHERE id = :id"), {"id": lease}
        ).scalar_one()
        == hours(0, 100)[1]
    )


def test_claims_cannot_drift_from_their_allocation(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocation_id = allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    conn.commit()
    start, end = hours(20, 21)
    attempts = [
        (
            "UPDATE allocation_claims SET period = tstzrange(:s, :e, '[)')"
            " WHERE allocation_id = :id",
            {"s": start, "e": end, "id": allocation_id},
        ),
        (
            "UPDATE allocation_claims SET is_active = false WHERE allocation_id = :id",
            {"id": allocation_id},
        ),
    ]
    for sql, params in attempts:
        with pytest.raises(IntegrityError) as raised:
            conn.execute(text(sql), params)
        assert raised.value.orig.sqlstate == "23503"
        assert raised.value.orig.diag.constraint_name == "fk_claims_allocation"
        conn.rollback()


@pytest.mark.parametrize(
    ("period_sql", "source", "constraint"),
    [
        ("tstzrange(:s, :s, '[)')", "BOOKING", "ck_allocations_period_shape"),  # empty
        ("tstzrange(:s, :e, '[]')", "BOOKING", "ck_allocations_period_shape"),
        ("tstzrange(:s, :e, '()')", "BOOKING", "ck_allocations_period_shape"),
        ("tstzrange(NULL, :e, '[)')", "LEASE", "ck_allocations_period_shape"),
        ("tstzrange(:s, NULL, '[)')", "BOOKING", "ck_allocations_source"),  # open-ended booking
    ],
)
def test_malformed_periods_are_rejected(
    conn: Connection, period_sql: str, source: str, constraint: str
) -> None:
    inv = build_inventory(conn)
    start, end = hours(9, 10)
    column = "booking_id" if source == "BOOKING" else "lease_space_id"
    with pytest.raises(IntegrityError) as raised:
        conn.execute(
            text(
                f"INSERT INTO allocations (source_type, {column}, property_id, space_id, period,"
                f" original_period) VALUES (:source, gen_random_uuid(), :p, :sp, {period_sql},"
                " tstzrange(:s, :e, '[)'))"
            ),
            {"source": source, "p": inv.property_id, "sp": inv.d1, "s": start, "e": end},
        )
    assert raised.value.orig.diag.constraint_name == constraint


def test_an_allocation_has_exactly_one_exclusive_claim(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocation_id = insert_allocation(conn, inv.property_id, inv.d1, *hours(9, 10))
    with pytest.raises(IntegrityError) as raised:
        conn.execute(
            text(
                "INSERT INTO allocation_claims (allocation_id, period, is_active, space_id,"
                " claim_mode) SELECT id, period, is_active, unnest(CAST(:spaces AS uuid[])), 1"
                " FROM allocations WHERE id = :id"
            ),
            {"id": allocation_id, "spaces": [inv.d1, inv.d2]},
        )
    assert raised.value.orig.diag.constraint_name == "uq_claims_one_exclusive"
