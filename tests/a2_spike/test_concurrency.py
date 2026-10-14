"""Races between confirmations, reschedules and inventory changes, on real connections."""

import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from tests.a2_spike.helpers import (
    EXCLUSIVE,
    SHARED,
    add_space,
    allocate,
    backend_pid,
    build_inventory,
    claim_chain,
    hours,
    insert_allocation,
    insert_claims,
    outcome,
    race,
    wait_until_blocked,
)

EXCLUSIVE_CONFLICT = "23P01:ex_claims_exclusive"
MIXED = "23P01:ex_claims_mixed"


class Worker:
    """One connection driven from its own thread, so a statement can block."""

    def __init__(self, engine: Engine) -> None:
        self.conn = engine.connect()
        self.pid = backend_pid(self.conn)
        self.conn.rollback()
        self._pool = ThreadPoolExecutor(max_workers=1)

    def submit(self, action: Callable[[Connection], object]) -> "Future[str]":
        return self._pool.submit(outcome, lambda: action(self.conn))

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        self.conn.rollback()
        self.conn.close()


@pytest.fixture
def worker(engine: Engine) -> Iterator[Callable[[], Worker]]:
    workers: list[Worker] = []

    def make() -> Worker:
        workers.append(Worker(engine))
        return workers[-1]

    yield make
    for item in workers:
        item.close()


PROPERTY_LOCKS = [("allocations", "trg_allocations_lock"), ("spaces", "trg_spaces_lock")]
CLAIM_LOCKS = [("allocation_claims", "trg_claims_lock")]


@contextmanager
def triggers_disabled(conn: Connection, triggers: list[tuple[str, str]]) -> Iterator[None]:
    """Control experiments: run with part of the mechanism switched off."""
    for table, trigger in triggers:
        conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        for table, trigger in triggers:
            conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
        conn.commit()


def active_targets(conn: Connection) -> set[uuid.UUID]:
    conn.rollback()
    return set(
        conn.execute(text("SELECT space_id FROM allocations WHERE is_active")).scalars().all()
    )


# --- concurrent confirmations ---------------------------------------------------


def test_many_requests_for_one_desk_yield_exactly_one_allocation(
    engine: Engine, conn: Connection
) -> None:
    inv = build_inventory(conn)
    results = race(
        engine,
        [lambda c: allocate(c, inv.property_id, inv.d1, *hours(9, 10)) for _ in range(12)],
    )
    assert results.count("ok") == 1
    assert set(results) == {"ok", EXCLUSIVE_CONFLICT}
    assert conn.execute(text("SELECT count(*) FROM allocations")).scalar_one() == 1


def test_sibling_desks_are_all_allocated_concurrently(engine: Engine, conn: Connection) -> None:
    inv = build_inventory(conn)
    desks = [
        add_space(conn, inv.property_id, inv.floor_id, inv.p1, "DESK", f"Hot desk {n}")
        for n in range(12)
    ]
    conn.commit()
    results = race(
        engine,
        [
            (lambda c, desk=desk: allocate(c, inv.property_id, desk, *hours(9, 10)))
            for desk in desks
        ],
    )
    assert results == ["ok"] * 12


def floor_against_desks(engine: Engine, conn: Connection, rounds: int) -> list[list[str]]:
    """Race one floor lease against three desk bookings per round; return each round's outcomes."""
    inv = build_inventory(conn)
    rounds_seen = []
    for round_number in range(rounds):
        period = hours(round_number * 2, round_number * 2 + 1)
        rounds_seen.append(
            race(
                engine,
                [
                    lambda c, period=period: allocate(
                        c, inv.property_id, inv.root, *period, source="LEASE"
                    )
                ]
                + [
                    (lambda c, d=desk, period=period: allocate(c, inv.property_id, d, *period))
                    for desk in (inv.d1, inv.d2, inv.d3)
                ],
            )
        )
    # Independent of the reported outcomes: no period holds both a floor and a desk.
    overlaps = conn.execute(
        text(
            "SELECT count(*) FROM allocations f JOIN allocations d"
            " ON f.period && d.period AND d.space_id <> f.space_id"
            " WHERE f.space_id = :root AND f.is_active AND d.is_active"
        ),
        {"root": inv.root},
    ).scalar_one()
    assert overlaps == 0
    return rounds_seen


def test_floor_lease_and_desk_bookings_never_both_succeed(engine: Engine, conn: Connection) -> None:
    for results in floor_against_desks(engine, conn, rounds=40):
        floor, desk_results = results[0], results[1:]
        # No deadlocks: every loser is told about the real conflict.
        assert set(results) <= {"ok", MIXED}, results
        if floor == "ok":
            assert desk_results == [MIXED] * 3, results
        else:
            # The floor lost to at least one desk; no desk can lose to another desk.
            assert desk_results == ["ok"] * 3, results


def test_without_claim_locks_the_race_stays_safe_but_can_deadlock(
    engine: Engine, conn: Connection
) -> None:
    """Control experiment for ``trg_claims_lock``: the constraints alone keep the
    data correct, but a loser may be aborted as a deadlock victim instead of
    being told about the conflict, and a compatible desk booking can be a victim."""
    with triggers_disabled(conn, CLAIM_LOCKS):
        rounds_seen = floor_against_desks(engine, conn, rounds=40)
    outcomes = {result for results in rounds_seen for result in results}
    assert outcomes <= {"ok", MIXED, "40P01:None"}, outcomes
    deadlocked = sum("40P01:None" in results for results in rounds_seen)
    # Shown with `pytest -s`; how often it happens depends on timing, so it is not asserted.
    print(f"without trg_claims_lock: {deadlocked} of {len(rounds_seen)} rounds deadlocked")


@pytest.mark.parametrize("first_commits", [True, False])
def test_second_confirmation_waits_for_the_first_to_finish(
    engine: Engine, conn: Connection, worker: Callable[[], Worker], first_commits: bool
) -> None:
    inv = build_inventory(conn)
    desk, floor = worker(), worker()
    allocate(desk.conn, inv.property_id, inv.d1, *hours(9, 11))  # not committed yet

    pending = floor.submit(
        lambda c: allocate(c, inv.property_id, inv.root, *hours(10, 12), source="LEASE")
    )
    assert wait_until_blocked(engine, floor.pid) == [desk.pid]

    if first_commits:
        desk.conn.commit()
        assert pending.result(timeout=20) == MIXED
    else:
        desk.conn.rollback()
        assert pending.result(timeout=20) == "ok"
        floor.conn.commit()
        assert active_targets(conn) == {inv.root}


def test_concurrent_reschedules_into_one_slot_let_exactly_one_through(
    engine: Engine, conn: Connection
) -> None:
    inv = build_inventory(conn)
    for round_number in range(10):
        base = round_number * 10
        first = allocate(conn, inv.property_id, inv.d1, *hours(base, base + 1))
        second = allocate(conn, inv.property_id, inv.d1, *hours(base + 2, base + 3))
        conn.commit()
        start, end = hours(base + 5, base + 6)

        def reschedule(c: Connection, allocation_id: uuid.UUID) -> None:
            c.execute(
                text("UPDATE allocations SET period = tstzrange(:s, :e, '[)') WHERE id = :id"),
                {"s": start, "e": end, "id": allocation_id},  # noqa: B023
            )

        results = race(
            engine,
            [lambda c: reschedule(c, first), lambda c: reschedule(c, second)],  # noqa: B023
        )
        assert sorted(results) == sorted(["ok", EXCLUSIVE_CONFLICT]), results
        moved = conn.execute(
            text("SELECT count(*) FROM allocations WHERE lower(period) = :s"), {"s": start}
        ).scalar_one()
        assert moved == 1
        conn.commit()


# --- confirmations against inventory changes ------------------------------------


def move_desk(c: Connection, desk: uuid.UUID, parent: uuid.UUID) -> None:
    c.execute(
        text("UPDATE spaces SET parent_space_id = :parent WHERE id = :id"),
        {"parent": parent, "id": desk},
    )


def test_move_waits_for_an_in_flight_confirmation_and_is_then_rejected(
    engine: Engine, conn: Connection, worker: Callable[[], Worker]
) -> None:
    inv = build_inventory(conn)
    confirm, mover = worker(), worker()
    allocate(confirm.conn, inv.property_id, inv.d1, *hours(9, 10))  # not committed yet

    def move_and_commit(c: Connection) -> None:
        move_desk(c, inv.d1, inv.p2)
        c.commit()

    pending = mover.submit(move_and_commit)
    assert wait_until_blocked(engine, mover.pid) == [confirm.pid]

    confirm.conn.commit()
    # The move's own statement started before the claims were committed; its
    # deferred guard still sees them at commit.
    assert pending.result(timeout=20) == "23514:ck_spaces_move_blocked"
    assert claim_chain(conn, inv.d1)[1] == (inv.p1, SHARED)


def test_confirmation_with_a_stale_chain_waits_for_the_move_and_fails_at_commit(
    engine: Engine, conn: Connection, worker: Callable[[], Worker]
) -> None:
    inv = build_inventory(conn)
    stale_chain = claim_chain(conn, inv.d1)  # root, P1, D1 - read before the move
    conn.rollback()
    mover, confirm = worker(), worker()
    move_desk(mover.conn, inv.d1, inv.p2)  # not committed yet

    def confirm_with_stale_chain(c: Connection) -> None:
        allocation_id = insert_allocation(c, inv.property_id, inv.d1, *hours(9, 10))
        insert_claims(c, allocation_id, stale_chain)
        c.commit()

    pending = confirm.submit(confirm_with_stale_chain)
    assert wait_until_blocked(engine, confirm.pid) == [mover.pid]

    mover.conn.commit()
    assert pending.result(timeout=20) == "23514:ck_allocation_claim_chain"
    assert active_targets(conn) == set()


def test_confirmation_that_locks_first_reads_the_moved_chain_and_succeeds(
    engine: Engine, conn: Connection, worker: Callable[[], Worker]
) -> None:
    inv = build_inventory(conn)
    mover, confirm = worker(), worker()
    move_desk(mover.conn, inv.d1, inv.p2)  # not committed yet

    def confirm_properly(c: Connection) -> None:
        allocate(c, inv.property_id, inv.d1, *hours(9, 10))
        c.commit()

    pending = confirm.submit(confirm_properly)
    assert wait_until_blocked(engine, confirm.pid) == [mover.pid]

    mover.conn.commit()
    assert pending.result(timeout=20) == "ok"
    claimed = conn.execute(
        text("SELECT space_id, claim_mode FROM allocation_claims ORDER BY claim_mode DESC")
    ).all()
    assert {(row.space_id, row.claim_mode) for row in claimed} == {
        (inv.root, SHARED),
        (inv.p2, SHARED),
        (inv.d1, EXCLUSIVE),
    }


def test_without_any_locks_the_race_goes_undetected(
    conn: Connection, worker: Callable[[], Worker]
) -> None:
    """Control experiment: the deferred checks alone do not close the race."""
    inv = build_inventory(conn)
    confirm, mover = worker(), worker()
    with triggers_disabled(conn, PROPERTY_LOCKS + CLAIM_LOCKS):
        try:
            allocation_id = insert_allocation(confirm.conn, inv.property_id, inv.d1, *hours(9, 10))
            insert_claims(confirm.conn, allocation_id, claim_chain(confirm.conn, inv.d1))
            move_desk(mover.conn, inv.d1, inv.p2)  # does not block: nothing is locked

            # Run both sets of deferred checks while neither transaction has committed,
            # which is what overlapping commits amount to. Each is blind to the other.
            for connection in (confirm.conn, mover.conn):
                connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert outcome(confirm.conn.commit) == "ok"
            assert outcome(mover.conn.commit) == "ok"

            # The committed allocation now claims P1 although the desk sits under P2.
            claimed = set(
                conn.execute(
                    text("SELECT space_id FROM allocation_claims WHERE allocation_id = :id"),
                    {"id": allocation_id},
                ).scalars()
            )
            assert inv.p1 in claimed and inv.p2 not in claimed
            assert claim_chain(conn, inv.d1)[1] == (inv.p2, SHARED)
        finally:
            # Release every lock, or re-enabling the triggers would wait on them.
            confirm.conn.rollback()
            mover.conn.rollback()


@pytest.mark.parametrize(
    "disabled", [PROPERTY_LOCKS, CLAIM_LOCKS], ids=["claim_locks_only", "property_locks_only"]
)
def test_either_lock_layer_alone_makes_the_move_wait_and_fail(
    engine: Engine,
    conn: Connection,
    worker: Callable[[], Worker],
    disabled: list[tuple[str, str]],
) -> None:
    inv = build_inventory(conn)
    confirm, mover = worker(), worker()
    with triggers_disabled(conn, disabled):
        try:
            allocation_id = insert_allocation(confirm.conn, inv.property_id, inv.d1, *hours(9, 10))
            insert_claims(confirm.conn, allocation_id, claim_chain(confirm.conn, inv.d1))

            def move_and_commit(c: Connection) -> None:
                move_desk(c, inv.d1, inv.p2)
                c.commit()

            pending = mover.submit(move_and_commit)
            assert wait_until_blocked(engine, mover.pid) == [confirm.pid]
            confirm.conn.commit()
            assert pending.result(timeout=20) == "23514:ck_spaces_move_blocked"
        finally:
            confirm.conn.rollback()
            mover.conn.rollback()


# --- multi-space leases ---------------------------------------------------------


def test_leases_claiming_spaces_in_opposite_order_deadlock_and_one_is_aborted(
    engine: Engine, conn: Connection, worker: Callable[[], Worker]
) -> None:
    inv = build_inventory(conn)
    one, two = worker(), worker()
    period = hours(0, 24)
    allocate(one.conn, inv.property_id, inv.p1, *period, source="LEASE")
    allocate(two.conn, inv.property_id, inv.p2, *period, source="LEASE")

    def finish(c: Connection, space: uuid.UUID) -> None:
        allocate(c, inv.property_id, space, *period, source="LEASE")
        c.commit()

    errors: list[BaseException] = []

    def capture(c: Connection, space: uuid.UUID) -> None:
        try:
            finish(c, space)
        except Exception as exc:
            errors.append(exc)
            raise

    first = one.submit(lambda c: capture(c, inv.p2))
    assert wait_until_blocked(engine, one.pid) == [two.pid]
    second = two.submit(lambda c: capture(c, inv.p1))

    results = sorted([first.result(timeout=30), second.result(timeout=30)])
    # PostgreSQL aborts one transaction; the survivor then hits the real conflict or commits.
    assert results[0] == "40P01:None", results
    assert results[1] in {"ok", EXCLUSIVE_CONFLICT}, results
    deadlock = next(exc for exc in errors if getattr(exc.orig, "sqlstate", "") == "40P01")
    assert isinstance(deadlock, OperationalError)
    assert not isinstance(deadlock, IntegrityError)


def test_leases_claiming_spaces_in_the_same_order_do_not_deadlock(
    engine: Engine, conn: Connection, worker: Callable[[], Worker]
) -> None:
    inv = build_inventory(conn)
    one, two = worker(), worker()
    period = hours(0, 24)
    allocate(one.conn, inv.property_id, inv.p1, *period, source="LEASE")

    def both(c: Connection) -> None:
        allocate(c, inv.property_id, inv.p1, *period, source="LEASE")
        allocate(c, inv.property_id, inv.p2, *period, source="LEASE")
        c.commit()

    pending = two.submit(both)
    assert wait_until_blocked(engine, two.pid) == [one.pid]
    allocate(one.conn, inv.property_id, inv.p2, *period, source="LEASE")
    one.conn.commit()
    assert pending.result(timeout=20) == EXCLUSIVE_CONFLICT
    assert active_targets(conn) == {inv.p1, inv.p2}
