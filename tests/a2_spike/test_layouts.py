"""Layout versions: one active layout per floor, and activation under concurrency."""

import uuid

import pytest
from sqlalchemy import Connection, Engine, text

from tests.a2_spike.helpers import add_floor, add_property, outcome, race

ONE_ACTIVE = "23505:uq_layouts_one_active_per_floor"


def add_layout(
    conn: Connection, floor_id: uuid.UUID | None, version: int | None, status: str = "DRAFT"
) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO layouts (floor_id, name, version_number, status)"
            " VALUES (:f, :name, :v, :status) RETURNING id"
        ),
        {"f": floor_id, "name": f"Plan v{version}", "v": version, "status": status},
    ).scalar_one()


def archive_active(conn: Connection, floor_id: uuid.UUID) -> None:
    conn.execute(
        text(
            "UPDATE layouts SET status = 'ARCHIVED', superseded_at = now()"
            " WHERE floor_id = :f AND status = 'ACTIVE'"
        ),
        {"f": floor_id},
    )


def set_active(conn: Connection, layout_id: uuid.UUID) -> None:
    conn.execute(text("UPDATE layouts SET status = 'ACTIVE' WHERE id = :id"), {"id": layout_id})


def statuses(conn: Connection, floor_id: uuid.UUID) -> list[str]:
    conn.rollback()
    return list(
        conn.execute(
            text("SELECT status FROM layouts WHERE floor_id = :f ORDER BY version_number"),
            {"f": floor_id},
        ).scalars()
    )


@pytest.fixture
def floor(conn: Connection) -> uuid.UUID:
    floor_id = add_floor(conn, add_property(conn))
    conn.commit()
    return floor_id


def test_a_floor_cannot_have_two_active_layouts(conn: Connection, floor: uuid.UUID) -> None:
    add_layout(conn, floor, 1, "ACTIVE")
    assert outcome(lambda: add_layout(conn, floor, 2, "ACTIVE")) == ONE_ACTIVE


def test_version_numbers_are_unique_per_floor(conn: Connection, floor: uuid.UUID) -> None:
    add_layout(conn, floor, 1)
    assert outcome(lambda: add_layout(conn, floor, 1)) == "23505:uq_layouts_floor_version"


def test_legacy_layouts_without_a_floor_are_not_constrained(conn: Connection) -> None:
    for _ in range(3):
        add_layout(conn, None, None, "ACTIVE")
    assert outcome(conn.commit) == "ok"


def test_activation_archives_then_activates_and_keeps_history(
    conn: Connection, floor: uuid.UUID
) -> None:
    add_layout(conn, floor, 1, "ACTIVE")
    draft = add_layout(conn, floor, 2)
    conn.commit()
    archive_active(conn, floor)
    set_active(conn, draft)
    conn.commit()
    assert statuses(conn, floor) == ["ARCHIVED", "ACTIVE"]


def test_activating_before_archiving_fails_because_the_index_is_not_deferrable(
    conn: Connection, floor: uuid.UUID
) -> None:
    add_layout(conn, floor, 1, "ACTIVE")
    draft = add_layout(conn, floor, 2)
    conn.commit()
    assert outcome(lambda: set_active(conn, draft)) == ONE_ACTIVE


def activation_round(
    engine: Engine, conn: Connection, floor: uuid.UUID, version: int, *, lock_floor: bool
) -> list[str]:
    drafts = [add_layout(conn, floor, version), add_layout(conn, floor, version + 1)]
    conn.commit()

    def activate(c: Connection, layout_id: uuid.UUID) -> None:
        if lock_floor:
            c.execute(text("SELECT 1 FROM floors WHERE id = :f FOR UPDATE"), {"f": floor})
        archive_active(c, floor)
        set_active(c, layout_id)

    return race(engine, [lambda c, d=draft: activate(c, d) for draft in drafts])


def test_concurrent_activations_without_a_lock_leave_exactly_one_active(
    engine: Engine, conn: Connection, floor: uuid.UUID
) -> None:
    add_layout(conn, floor, 1, "ACTIVE")
    seen: set[str] = set()
    for round_number in range(20):
        results = activation_round(engine, conn, floor, 2 + round_number * 2, lock_floor=False)
        seen.update(results)
        # The index decides: a loser is rejected, never a second active layout.
        assert set(results) <= {"ok", ONE_ACTIVE}, results
        assert "ok" in results
        assert statuses(conn, floor).count("ACTIVE") == 1
    print(f"outcomes without the floor lock: {sorted(seen)}")


def test_concurrent_activations_with_the_floor_lock_both_succeed_in_turn(
    engine: Engine, conn: Connection, floor: uuid.UUID
) -> None:
    add_layout(conn, floor, 1, "ACTIVE")
    for round_number in range(20):
        results = activation_round(engine, conn, floor, 2 + round_number * 2, lock_floor=True)
        assert results == ["ok", "ok"], results
        assert statuses(conn, floor).count("ACTIVE") == 1
    assert statuses(conn, floor).count("DRAFT") == 0
