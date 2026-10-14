"""Deferred triggers: chain completeness, the inventory guard, and rollback on failure."""

import uuid

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.a2_spike.helpers import (
    EXCLUSIVE,
    SHARED,
    Inventory,
    add_floor,
    add_space,
    allocate,
    build_inventory,
    claim_chain,
    hours,
    insert_allocation,
    insert_claims,
    outcome,
)

CHAIN = "23514:ck_allocation_claim_chain"
MOVE_BLOCKED = "23514:ck_spaces_move_blocked"
CHANGE_BLOCKED = "23514:ck_spaces_change_blocked"


def count(conn: Connection, table: str) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def move(conn: Connection, space_id: uuid.UUID, parent: uuid.UUID) -> None:
    conn.execute(
        text(
            "UPDATE spaces s SET parent_space_id = p.id, floor_id = p.floor_id,"
            " parent_depth = p.depth, depth = p.depth + 1 FROM spaces p"
            " WHERE s.id = :id AND p.id = :parent"
        ),
        {"id": space_id, "parent": parent},
    )


# --- claim chain completeness -------------------------------------------------


@pytest.mark.parametrize(
    "broken_chain",
    ["no_claims", "missing_ancestor", "shared_on_target_only", "extra_space", "wrong_target"],
)
def test_incomplete_chain_fails_at_commit_and_rolls_everything_back(
    conn: Connection, broken_chain: str
) -> None:
    inv = build_inventory(conn)
    chains = {
        "no_claims": [],
        "missing_ancestor": [(inv.root, SHARED), (inv.d1, EXCLUSIVE)],
        "shared_on_target_only": [(inv.root, SHARED), (inv.p1, SHARED), (inv.d1, SHARED)],
        "extra_space": [(inv.root, SHARED), (inv.p1, SHARED), (inv.p2, SHARED), (inv.d1, 1)],
        "wrong_target": [(inv.root, SHARED), (inv.p1, SHARED), (inv.d2, EXCLUSIVE)],
    }
    allocation_id = insert_allocation(conn, inv.property_id, inv.d1, *hours(9, 10))
    insert_claims(conn, allocation_id, chains[broken_chain])
    # Nothing is checked until commit: the statements above all succeeded.
    assert count(conn, "allocations") == 1

    assert outcome(conn.commit) == CHAIN
    conn.rollback()
    assert count(conn, "allocations") == 0
    assert count(conn, "allocation_claims") == 0


def test_complete_chain_commits(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    assert outcome(conn.commit) == "ok"
    assert count(conn, "allocation_claims") == 3


def test_deleting_a_claim_of_a_live_allocation_fails_at_commit(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocation_id = allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    conn.commit()
    conn.execute(
        text("DELETE FROM allocation_claims WHERE allocation_id = :id AND space_id = :s"),
        {"id": allocation_id, "s": inv.root},
    )
    assert outcome(conn.commit) == CHAIN
    conn.rollback()
    assert count(conn, "allocation_claims") == 3


def test_chain_check_can_be_run_early_with_set_constraints(conn: Connection) -> None:
    inv = build_inventory(conn)
    insert_allocation(conn, inv.property_id, inv.d1, *hours(9, 10))
    assert outcome(lambda: conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))) == CHAIN


def test_past_and_released_allocations_are_exempt(conn: Connection) -> None:
    inv = build_inventory(conn)
    past = conn.execute(
        text(
            "INSERT INTO allocations (source_type, booking_id, property_id, space_id, period,"
            " original_period) VALUES ('BOOKING', gen_random_uuid(), :p, :s,"
            " tstzrange(now() - interval '2 days', now() - interval '1 day', '[)'),"
            " tstzrange(now() - interval '2 days', now() - interval '1 day', '[)')) RETURNING id"
        ),
        {"p": inv.property_id, "s": inv.d1},
    ).scalar_one()
    assert outcome(conn.commit) == "ok"

    live = allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    conn.commit()
    conn.execute(
        text("UPDATE allocations SET is_active = false, released_at = now() WHERE id = :id"),
        {"id": live},
    )
    assert outcome(conn.commit) == "ok"
    assert past != live


# --- inventory guard ------------------------------------------------------------


def test_moving_a_space_with_a_future_allocation_is_rejected(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    conn.commit()
    move(conn, inv.d1, inv.p2)
    assert outcome(conn.commit) == MOVE_BLOCKED
    conn.rollback()
    assert (
        conn.execute(
            text("SELECT parent_space_id FROM spaces WHERE id = :id"), {"id": inv.d1}
        ).scalar_one()
        == inv.p1
    )


def test_moving_an_ancestor_of_an_allocated_space_is_rejected(conn: Connection) -> None:
    """The portion holds only a shared claim, which is enough to block the move."""
    inv = build_inventory(conn)
    other_floor = add_floor(conn, inv.property_id, "Floor 2")
    other_root = add_space(conn, inv.property_id, other_floor, None, "FLOOR", "Whole floor 2")
    allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    conn.commit()
    move(conn, inv.p1, other_root)
    conn.execute(
        text("UPDATE spaces SET floor_id = :f WHERE parent_space_id = :p"),
        {"f": other_floor, "p": inv.p1},
    )
    assert outcome(conn.commit) == MOVE_BLOCKED


def test_moving_an_unallocated_subtree_works_with_the_deferred_parent_key(
    conn: Connection,
) -> None:
    inv = build_inventory(conn)
    other_floor = add_floor(conn, inv.property_id, "Floor 2")
    other_root = add_space(conn, inv.property_id, other_floor, None, "FLOOR", "Whole floor 2")
    conn.commit()

    # Parent first: its children briefly reference a (floor, depth) that no longer matches.
    move(conn, inv.p1, other_root)
    conn.execute(
        text("UPDATE spaces SET floor_id = :f WHERE parent_space_id = :p"),
        {"f": other_floor, "p": inv.p1},
    )
    assert outcome(conn.commit) == "ok"
    assert claim_chain(conn, inv.d1) == [
        (other_root, SHARED),
        (inv.p1, SHARED),
        (inv.d1, EXCLUSIVE),
    ]


def test_subtree_move_that_leaves_children_behind_fails_at_commit(conn: Connection) -> None:
    inv = build_inventory(conn)
    other_floor = add_floor(conn, inv.property_id, "Floor 2")
    other_root = add_space(conn, inv.property_id, other_floor, None, "FLOOR", "Whole floor 2")
    conn.commit()
    move(conn, inv.p1, other_root)  # D1 and D2 still claim the old floor
    assert outcome(conn.commit) == "23503:fk_spaces_parent"


def test_moving_is_allowed_once_allocations_are_past_or_released(conn: Connection) -> None:
    inv = build_inventory(conn)
    allocation_id = allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    conn.commit()
    conn.execute(
        text("UPDATE allocations SET is_active = false, released_at = now() WHERE id = :id"),
        {"id": allocation_id},
    )
    conn.commit()
    move(conn, inv.d1, inv.p2)
    assert outcome(conn.commit) == "ok"


@pytest.mark.parametrize(
    ("target", "change", "expected"),
    [
        ("d1", "archived_at = now()", CHANGE_BLOCKED),
        ("d1", "operational_status = 'UNDER_MAINTENANCE'", CHANGE_BLOCKED),
        ("d1", "is_rentable = false", CHANGE_BLOCKED),
        # Ancestors hold shared claims: they cannot be archived or closed either...
        ("p1", "archived_at = now()", CHANGE_BLOCKED),
        ("root", "operational_status = 'INACTIVE'", CHANGE_BLOCKED),
        # ...but may stop being rentable themselves, and may be renamed.
        ("p1", "is_rentable = false", "ok"),
        ("root", "name = 'Renamed'", "ok"),
        ("d1", "name = 'Renamed'", "ok"),
        # Unrelated spaces are unaffected.
        ("d3", "archived_at = now()", "ok"),
        ("p2", "operational_status = 'INACTIVE'", "ok"),
    ],
)
def test_guard_blocks_only_changes_that_conflict_with_allocations(
    conn: Connection, target: str, change: str, expected: str
) -> None:
    inv = build_inventory(conn)
    allocate(conn, inv.property_id, inv.d1, *hours(9, 10))
    conn.commit()
    conn.execute(text(f"UPDATE spaces SET {change} WHERE id = :id"), {"id": getattr(inv, target)})
    assert outcome(conn.commit) == expected


def test_a_space_cannot_change_property(conn: Connection) -> None:
    inv = build_inventory(conn)
    other = conn.execute(
        text("INSERT INTO properties (name) VALUES ('Other') RETURNING id")
    ).scalar_one()
    result = outcome(
        lambda: conn.execute(
            text("UPDATE spaces SET property_id = :p WHERE id = :id"), {"p": other, "id": inv.d1}
        )
    )
    assert result == "23514:ck_spaces_property_immutable"


# --- first assignment of a legacy space ----------------------------------------


def legacy_room_with_booking(conn: Connection, inv: Inventory) -> tuple[uuid.UUID, uuid.UUID]:
    room = add_space(conn, inv.property_id, None, None, "MEETING_ROOM", "Legacy room")
    allocation_id = allocate(conn, inv.property_id, room, *hours(9, 10))
    conn.commit()
    return room, allocation_id


def assign(conn: Connection, room: uuid.UUID, parent: uuid.UUID) -> None:
    conn.execute(
        text(
            "UPDATE spaces s SET floor_id = p.floor_id, parent_space_id = p.id,"
            " parent_depth = p.depth, depth = p.depth + 1 FROM spaces p"
            " WHERE s.id = :id AND p.id = :parent"
        ),
        {"id": room, "parent": parent},
    )


def test_assigning_a_booked_legacy_space_requires_rewritten_claims(conn: Connection) -> None:
    inv = build_inventory(conn)
    room, _ = legacy_room_with_booking(conn, inv)
    assign(conn, room, inv.p2)
    assert outcome(conn.commit) == CHAIN
    conn.rollback()
    assert (
        conn.execute(text("SELECT floor_id FROM spaces WHERE id = :id"), {"id": room}).scalar_one()
        is None
    )


def test_assigning_a_booked_legacy_space_succeeds_with_rewritten_claims(
    conn: Connection,
) -> None:
    inv = build_inventory(conn)
    room, allocation_id = legacy_room_with_booking(conn, inv)
    assign(conn, room, inv.p2)
    insert_claims(conn, allocation_id, [(inv.root, SHARED), (inv.p2, SHARED)])
    assert outcome(conn.commit) == "ok"
    # The booking now blocks a lease of the floor it sits on.
    assert (
        outcome(lambda: allocate(conn, inv.property_id, inv.root, *hours(9, 10), source="LEASE"))
        == "23P01:ex_claims_mixed"
    )


def test_assigning_under_an_already_leased_floor_is_rejected(conn: Connection) -> None:
    inv = build_inventory(conn)
    room, allocation_id = legacy_room_with_booking(conn, inv)
    allocate(conn, inv.property_id, inv.root, *hours(0, 24), source="LEASE")
    conn.commit()
    assign(conn, room, inv.p2)
    result = outcome(
        lambda: insert_claims(conn, allocation_id, [(inv.root, SHARED), (inv.p2, SHARED)])
    )
    assert result == "23P01:ex_claims_mixed"


# --- hierarchy constraints ------------------------------------------------------


def test_hierarchy_shape_is_enforced(conn: Connection) -> None:
    inv = build_inventory(conn)
    other_floor = add_floor(conn, inv.property_id, "Floor 2")
    other_root = add_space(conn, inv.property_id, other_floor, None, "FLOOR", "Whole floor 2")
    conn.commit()

    def insert(**values: object) -> str:
        row = {
            "property_id": inv.property_id,
            "floor_id": inv.floor_id,
            "parent_space_id": inv.root,
            "depth": 1,
            "parent_depth": 0,
            "name": "X",
            "space_type": "DESK",
        } | values

        def action() -> None:
            conn.execute(
                text(
                    "INSERT INTO spaces (property_id, floor_id, parent_space_id, depth,"
                    " parent_depth, name, space_type) VALUES (:property_id, :floor_id,"
                    " :parent_space_id, :depth, :parent_depth, :name, :space_type)"
                ),
                row,
            )
            conn.commit()

        result = outcome(action)
        conn.rollback()
        return result

    assert insert() == "ok"
    assert insert(space_type="FLOOR", parent_space_id=None, depth=0, parent_depth=None) == (
        "23505:uq_spaces_floor_root"
    )
    assert insert(parent_space_id=None, depth=0, parent_depth=None) == (
        "23514:ck_spaces_hierarchy_shape"
    )
    assert insert(depth=2) == "23514:ck_spaces_depth"
    assert insert(depth=3, parent_depth=2) == "23503:fk_spaces_parent"  # lies about the parent
    assert insert(parent_space_id=other_root) == "23503:fk_spaces_parent"  # other floor
    assert insert(parent_depth=None) == "23514:ck_spaces_parent_pair"
    assert insert(space_type="FLOOR") == "23514:ck_spaces_hierarchy_shape"
    assert insert(floor_id=None) == "23514:ck_spaces_hierarchy_shape"
    assert insert(floor_id=None, parent_space_id=None, depth=0, parent_depth=None) == "ok"


def test_a_cycle_cannot_be_created(conn: Connection) -> None:
    inv = build_inventory(conn)
    # Make P1 a child of its own child D1: depth must be both below and above D1's.
    for depth, parent_depth in [(3, 2), (1, 0)]:
        conn.execute(
            text(
                "UPDATE spaces SET parent_space_id = :parent, depth = :d, parent_depth = :pd"
                " WHERE id = :id"
            ),
            {"parent": inv.d1, "d": depth, "pd": parent_depth, "id": inv.p1},
        )
        assert outcome(conn.commit) == "23503:fk_spaces_parent"
        conn.rollback()


# --- isolation level ------------------------------------------------------------


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_allocation_writes_are_refused_outside_read_committed(engine: Engine, level: str) -> None:
    with engine.connect() as setup:
        inv = build_inventory(setup)
    with engine.connect().execution_options(isolation_level=level) as strict:
        with pytest.raises(DBAPIError) as raised:
            insert_allocation(strict, inv.property_id, inv.d1, *hours(9, 10))
        assert raised.value.orig.sqlstate == "25001"
        assert not isinstance(raised.value, IntegrityError)
