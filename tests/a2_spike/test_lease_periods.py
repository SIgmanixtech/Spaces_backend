"""Date-based lease terms converted to half-open intervals in the property's time zone."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Connection, text

from tests.a2_spike.helpers import allocate, build_inventory, outcome


def lease_period(conn: Connection, start: date, end: date | None, zone: str) -> tuple:
    return conn.execute(
        text(
            "SELECT lower(p), upper(p), upper_inf(p), lower_inc(p), upper_inc(p)"
            " FROM lease_period(:s, :e, :tz) AS p"
        ),
        {"s": start, "e": end, "tz": zone},
    ).one()


def test_inclusive_dates_become_a_half_open_interval_in_kolkata(conn: Connection) -> None:
    lower, upper, unbounded, lower_inc, upper_inc = lease_period(
        conn, date(2026, 11, 1), date(2027, 10, 31), "Asia/Kolkata"
    )
    assert lower == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)
    assert upper == datetime(2027, 10, 31, 18, 30, tzinfo=UTC)
    assert (unbounded, lower_inc, upper_inc) == (False, True, False)


def test_open_ended_lease_has_no_upper_bound(conn: Connection) -> None:
    lower, upper, unbounded, _, _ = lease_period(conn, date(2026, 11, 1), None, "Asia/Kolkata")
    assert lower == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)
    assert upper is None and unbounded


def test_single_day_lease_is_one_local_day(conn: Connection) -> None:
    lower, upper, *_ = lease_period(conn, date(2026, 11, 1), date(2026, 11, 1), "Asia/Kolkata")
    assert (upper - lower).total_seconds() == 24 * 3600


def test_consecutive_leases_do_not_conflict_but_a_shared_day_does(conn: Connection) -> None:
    inv = build_inventory(conn)

    def lease(start: date, end: date) -> str:
        def action() -> None:
            lower, upper, *_ = lease_period(conn, start, end, "Asia/Kolkata")
            with conn.begin_nested():
                allocate(conn, inv.property_id, inv.p1, lower, upper, source="LEASE")

        return outcome(action)

    assert lease(date(2031, 1, 1), date(2031, 12, 31)) == "ok"
    assert lease(date(2032, 1, 1), date(2032, 12, 31)) == "ok"  # starts the next day
    assert lease(date(2031, 12, 31), date(2031, 12, 31)) == "23P01:ex_claims_exclusive"


def test_termination_truncates_to_the_end_of_the_effective_day(conn: Connection) -> None:
    inv = build_inventory(conn)
    lower, upper, *_ = lease_period(conn, date(2031, 1, 1), date(2031, 12, 31), "Asia/Kolkata")
    lease = allocate(conn, inv.property_id, inv.p1, lower, upper, source="LEASE")
    conn.execute(
        text(
            "UPDATE allocations SET period = tstzrange(lower(period),"
            " upper(lease_period(:d, :d, 'Asia/Kolkata')), '[)') WHERE id = :id"
        ),
        {"d": date(2031, 6, 30), "id": lease},
    )
    conn.commit()

    def book(day: date) -> str:
        def action() -> None:
            day_lower, day_upper, *_ = lease_period(conn, day, day, "Asia/Kolkata")
            with conn.begin_nested():
                allocate(conn, inv.property_id, inv.d1, day_lower, day_upper)

        return outcome(action)

    assert book(date(2031, 6, 30)) == "23P01:ex_claims_mixed"  # last day of occupancy
    assert book(date(2031, 7, 1)) == "ok"


@pytest.mark.parametrize(
    "zone",
    [
        "Asia/Kolkata",
        "Europe/London",
        "America/New_York",
        "America/Havana",  # clocks go forward at local midnight
        "Africa/Cairo",  # clocks go forward at local midnight
        "Asia/Beirut",  # clocks go forward at local midnight
        "Australia/Lord_Howe",  # half-hour shift
    ],
)
def test_daily_periods_tile_the_year_without_gaps_or_overlaps(conn: Connection, zone: str) -> None:
    """Around daylight-saving changes a local midnight can be skipped or repeated."""
    row = conn.execute(
        text(
            "WITH days AS ("
            "  SELECT d::date AS day, lease_period(d::date, d::date, :tz) AS p,"
            "         lease_period(d::date + 1, d::date + 1, :tz) AS next_p"
            "  FROM generate_series(date '2026-01-01', date '2027-12-31', interval '1 day') d)"
            " SELECT count(*) AS days,"
            "  count(*) FILTER (WHERE isempty(p)) AS empty,"
            "  count(*) FILTER (WHERE upper(p) <> lower(next_p)) AS gaps_or_overlaps,"
            "  min(upper(p) - lower(p)) AS shortest, max(upper(p) - lower(p)) AS longest"
            " FROM days"
        ),
        {"tz": zone},
    ).one()
    assert row.days == 730
    assert row.empty == 0
    assert row.gaps_or_overlaps == 0
    assert 23 * 3600 <= row.shortest.total_seconds() <= 24 * 3600
    assert 24 * 3600 <= row.longest.total_seconds() <= 25 * 3600
    print(f"{zone}: shortest day {row.shortest}, longest day {row.longest}")
