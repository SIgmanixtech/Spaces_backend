"""Test helpers: build a small inventory, allocate against it, observe blocking."""

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

EXCLUSIVE, SHARED = 1, 2
# Far enough ahead that every period below is "future" for the triggers.
BASE = datetime(2031, 1, 6, tzinfo=UTC)


def hours(start: int, end: int) -> tuple[datetime, datetime]:
    return BASE + timedelta(hours=start), BASE + timedelta(hours=end)


@dataclass(frozen=True)
class Inventory:
    """One floor: root -> P1 -> (D1, D2), root -> P2 -> D3."""

    property_id: uuid.UUID
    floor_id: uuid.UUID
    root: uuid.UUID
    p1: uuid.UUID
    p2: uuid.UUID
    d1: uuid.UUID
    d2: uuid.UUID
    d3: uuid.UUID


def add_property(conn: Connection, name: str = "Test property") -> uuid.UUID:
    return conn.execute(
        text("INSERT INTO properties (name) VALUES (:name) RETURNING id"), {"name": name}
    ).scalar_one()


def add_floor(conn: Connection, property_id: uuid.UUID, name: str = "Floor 1") -> uuid.UUID:
    return conn.execute(
        text("INSERT INTO floors (property_id, name) VALUES (:p, :name) RETURNING id"),
        {"p": property_id, "name": name},
    ).scalar_one()


def add_space(
    conn: Connection,
    property_id: uuid.UUID,
    floor_id: uuid.UUID | None,
    parent: uuid.UUID | None,
    space_type: str,
    name: str,
) -> uuid.UUID:
    parent_depth = None
    if parent is not None:
        parent_depth = conn.execute(
            text("SELECT depth FROM spaces WHERE id = :id"), {"id": parent}
        ).scalar_one()
    return conn.execute(
        text(
            "INSERT INTO spaces (property_id, floor_id, parent_space_id, depth, parent_depth, "
            "name, space_type) VALUES (:p, :f, :parent, :depth, :pd, :name, :type) RETURNING id"
        ),
        {
            "p": property_id,
            "f": floor_id,
            "parent": parent,
            "depth": 0 if parent_depth is None else parent_depth + 1,
            "pd": parent_depth,
            "name": name,
            "type": space_type,
        },
    ).scalar_one()


def build_inventory(conn: Connection) -> Inventory:
    prop = add_property(conn)
    floor = add_floor(conn, prop)
    root = add_space(conn, prop, floor, None, "FLOOR", "Whole floor")
    p1 = add_space(conn, prop, floor, root, "PORTION", "P1")
    p2 = add_space(conn, prop, floor, root, "PORTION", "P2")
    d1 = add_space(conn, prop, floor, p1, "DESK", "D1")
    d2 = add_space(conn, prop, floor, p1, "DESK", "D2")
    d3 = add_space(conn, prop, floor, p2, "DESK", "D3")
    conn.commit()
    return Inventory(prop, floor, root, p1, p2, d1, d2, d3)


def claim_chain(conn: Connection, space_id: uuid.UUID) -> list[tuple[uuid.UUID, int]]:
    """The claims an allocation of ``space_id`` needs, root first."""
    rows = conn.execute(
        text(
            "WITH RECURSIVE chain AS ("
            "  SELECT id, parent_space_id, depth, 1 AS claim_mode FROM spaces WHERE id = :id"
            "  UNION ALL"
            "  SELECT p.id, p.parent_space_id, p.depth, 2 FROM spaces p"
            "  JOIN chain c ON p.id = c.parent_space_id)"
            " SELECT id, claim_mode FROM chain ORDER BY depth"
        ),
        {"id": space_id},
    ).all()
    return [(row.id, row.claim_mode) for row in rows]


def insert_allocation(
    conn: Connection,
    property_id: uuid.UUID,
    space_id: uuid.UUID,
    start: datetime,
    end: datetime | None,
    *,
    source: str = "BOOKING",
) -> uuid.UUID:
    column = "booking_id" if source == "BOOKING" else "lease_space_id"
    return conn.execute(
        text(
            f"INSERT INTO allocations (source_type, {column}, property_id, space_id, period, "
            "original_period) VALUES (:source, gen_random_uuid(), :p, :s, "
            "tstzrange(:start, :end, '[)'), tstzrange(:start, :end, '[)')) RETURNING id"
        ),
        {"source": source, "p": property_id, "s": space_id, "start": start, "end": end},
    ).scalar_one()


def insert_claims(
    conn: Connection, allocation_id: uuid.UUID, chain: list[tuple[uuid.UUID, int]]
) -> None:
    """All claims in one statement, copying period and status from the allocation."""
    conn.execute(
        text(
            "INSERT INTO allocation_claims (allocation_id, period, is_active, space_id, claim_mode)"
            " SELECT a.id, a.period, a.is_active, c.space_id, c.claim_mode"
            " FROM allocations a,"
            " unnest(CAST(:spaces AS uuid[]), CAST(:modes AS smallint[])) WITH ORDINALITY"
            "   AS c(space_id, claim_mode, position)"
            " WHERE a.id = :id ORDER BY c.position"
        ),
        {
            "id": allocation_id,
            "spaces": [space for space, _ in chain],
            "modes": [mode for _, mode in chain],
        },
    )


def allocate(
    conn: Connection,
    property_id: uuid.UUID,
    space_id: uuid.UUID,
    start: datetime,
    end: datetime | None,
    *,
    source: str = "BOOKING",
    chain: list[tuple[uuid.UUID, int]] | None = None,
) -> uuid.UUID:
    """The reference algorithm: lock the property, read the chain, write allocation and claims."""
    conn.execute(text("SELECT 1 FROM properties WHERE id = :p FOR KEY SHARE"), {"p": property_id})
    if chain is None:
        chain = claim_chain(conn, space_id)
    allocation_id = insert_allocation(conn, property_id, space_id, start, end, source=source)
    insert_claims(conn, allocation_id, chain)
    return allocation_id


def outcome(action: Callable[[], object]) -> str:
    """``"ok"`` or ``"<sqlstate>:<constraint name>"`` for a database error."""
    try:
        action()
    except DBAPIError as exc:
        return f"{exc.orig.sqlstate}:{exc.orig.diag.constraint_name}"
    return "ok"


def backend_pid(conn: Connection) -> int:
    return conn.execute(text("SELECT pg_backend_pid()")).scalar_one()


def wait_until_blocked(engine: Engine, pid: int, timeout: float = 10.0) -> list[int]:
    """Poll until backend ``pid`` waits on a lock. Returns the pids blocking it."""
    deadline = time.monotonic() + timeout
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while time.monotonic() < deadline:
            blockers = observer.execute(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
            ).scalar_one()
            if blockers:
                return list(blockers)
            time.sleep(0.05)
    raise AssertionError(f"backend {pid} never blocked")


def race(engine: Engine, actions: list[Callable[[Connection], object]]) -> list[str]:
    """Run every action at once, each in its own transaction, and commit it."""
    barrier = threading.Barrier(len(actions))

    def run(action: Callable[[Connection], object]) -> str:
        with engine.connect() as connection:
            barrier.wait(timeout=30)

            def work() -> None:
                action(connection)
                connection.commit()

            return outcome(work)

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))
