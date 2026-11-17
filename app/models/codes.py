"""Codes are generated, never typed (owner request of 16 Nov 2026).

A property, space or asset saved without a code gets the next one in its series, just
before it is written, whichever screen or API call created it:

- properties: ``PROP-0001``, ``PROP-0002``... (unique across all properties)
- spaces: a prefix for the type and a number within the property, e.g. ``CB-001`` for the
  first cabin, ``PT-002`` for the second portion, ``FL-001`` for a floor (unique within
  the property, as the database requires)
- assets: ``AST-00001``... in ``asset_tag`` (unique across all assets)

A code given explicitly (an import, an older client) is kept as it is. Numbers continue
after the highest existing code of the same series, so codes typed in the past are never
reused. A transaction-scoped advisory lock per series makes two simultaneous creations
take different numbers.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.enums import SpaceType
from app.models.property import Property
from app.models.space import Space

PROPERTY_PREFIX, PROPERTY_WIDTH = "PROP", 4
ASSET_PREFIX, ASSET_WIDTH = "AST", 5
SPACE_WIDTH = 3
SPACE_PREFIX: dict[str, str] = {
    SpaceType.DESK: "DK",
    SpaceType.PRIVATE_OFFICE: "PO",
    SpaceType.CABIN: "CB",
    SpaceType.MEETING_ROOM: "MR",
    SpaceType.COWORKING: "CW",
    SpaceType.FLOOR: "FL",
    SpaceType.PORTION: "PT",
    SpaceType.CONFERENCE_ROOM: "CR",
    SpaceType.TRAINING_ROOM: "TR",
    SpaceType.NON_RENTABLE: "NR",
    SpaceType.DEDICATED_WORKSTATION: "DW",
    SpaceType.FLEXIBLE_WORKSTATION: "FW",
    SpaceType.SEAT: "ST",
    SpaceType.OTHER: "SP",
    SpaceType.PARKING_SLOT: "PK",
}


def space_prefix(space_type: Any) -> str:
    return SPACE_PREFIX.get(str(getattr(space_type, "value", space_type)), "SP")


def _lock(session: Session, key: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


def _highest(session: Session, column: Any, prefix: str, *where: Any) -> int:
    pattern = re.compile(rf"^{prefix}-(\d+)$")
    found = session.scalars(select(column).where(column.like(f"{prefix}-%"), *where))
    return max((int(m.group(1)) for v in found if v and (m := pattern.match(v))), default=0)


def _natural(record: Any) -> list[Any]:
    name = str(getattr(record, "name", "") or "")
    parts = re.split(r"(\d+)", name)
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts]


def assign_codes(session: Session, _context: Any = None, _instances: Any = None) -> None:
    new = [o for o in session.new if isinstance(o, (Property, Space, Asset))]
    if not new:
        return
    with session.no_autoflush:
        counters: dict[tuple[str, ...], int] = {}

        def next_number(key: tuple[str, ...], column: Any, prefix: str, *where: Any) -> int:
            if key not in counters:
                _lock(session, "codes:" + ":".join(key))
                counters[key] = _highest(session, column, prefix, *where)
            counters[key] += 1
            return counters[key]

        # Codes given explicitly in this same flush are taken too.
        given: dict[tuple[str, ...], set[str]] = defaultdict(set)
        for o in new:
            if isinstance(o, Property) and o.code:
                given[("property",)].add(o.code)
            elif isinstance(o, Space) and o.code:
                given[("space", str(o.property_id))].add(o.code)
            elif isinstance(o, Asset) and o.asset_tag:
                given[("asset",)].add(o.asset_tag)

        def fresh(key: tuple[str, ...], column: Any, prefix: str, width: int, *where: Any) -> str:
            while True:
                code = f"{prefix}-{next_number(key + (prefix,), column, prefix, *where):0{width}d}"
                if code not in given[key]:
                    given[key].add(code)
                    return code

        # In name order, numbers compared as numbers ("Cabin 2" before "Cabin 10").
        for o in sorted(new, key=_natural):
            if isinstance(o, Property) and not o.code:
                o.code = fresh(("property",), Property.code, PROPERTY_PREFIX, PROPERTY_WIDTH)
            elif isinstance(o, Space) and not o.code and o.property_id is not None:
                o.code = fresh(
                    ("space", str(o.property_id)),
                    Space.code,
                    space_prefix(o.space_type),
                    SPACE_WIDTH,
                    Space.property_id == o.property_id,
                )
            elif isinstance(o, Asset) and not o.asset_tag:
                o.asset_tag = fresh(("asset",), Asset.asset_tag, ASSET_PREFIX, ASSET_WIDTH)


def register() -> None:
    if not event.contains(Session, "before_flush", assign_codes):
        event.listen(Session, "before_flush", assign_codes)
