"""The spaces around a space: those containing it and those inside it.

A booking or occupancy of a portion covers every desk and room in it, and one of
a desk is part of its portion and floor. Bookings and statuses look across this
family, not at the one space alone. The hierarchy is at most five levels deep.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.space import Space


@dataclass(frozen=True)
class Family:
    """Ids of the spaces containing a space (nearest first) and inside it."""

    ancestors: tuple[uuid.UUID, ...]
    descendants: frozenset[uuid.UUID]

    @property
    def related(self) -> set[uuid.UUID]:
        return {*self.ancestors, *self.descendants}

    def relation(self, space_id: uuid.UUID) -> str:
        """How another member relates to the space, for messages."""
        return "contains" if space_id in self.ancestors else "is inside"


def parent_map(
    session: Session, space_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Each space's parent, for the spaces given and every space above them."""
    parents: dict[uuid.UUID, uuid.UUID | None] = {}
    frontier = set(space_ids)
    while frontier:
        rows = session.execute(
            select(Space.id, Space.parent_space_id).where(Space.id.in_(frontier))
        ).all()
        for space_id, parent_id in rows:
            parents[space_id] = parent_id
        frontier = {p for _, p in rows if p is not None and p not in parents}
    return parents


def chain(parents: dict[uuid.UUID, uuid.UUID | None], space_id: uuid.UUID) -> list[uuid.UUID]:
    """The space's ancestors, nearest first."""
    out: list[uuid.UUID] = []
    current = parents.get(space_id)
    while current is not None and current not in out:
        out.append(current)
        current = parents.get(current)
    return out


def family(session: Session, space: Space) -> Family:
    ancestors = chain(parent_map(session, [space.id]), space.id)
    tree = select(Space.id).where(Space.parent_space_id == space.id).cte(recursive=True)
    tree = tree.union_all(select(Space.id).where(Space.parent_space_id == tree.c.id))
    descendants = frozenset(session.scalars(select(tree.c.id)))
    return Family(tuple(ancestors), descendants)
