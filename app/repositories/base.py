"""Generic data access: lookups, search, sorting and pagination."""

import uuid
from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import ColumnElement, Row, Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError
from app.db.base import Base
from app.schemas.common import ListQuery

ModelT = TypeVar("ModelT", bound=Base)


def text_search(columns: Sequence[ColumnElement[Any]], term: str) -> ColumnElement[bool]:
    """Case-insensitive "contains" match with LIKE wildcards in the term escaped."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return or_(*(column.ilike(f"%{escaped}%", escape="\\") for column in columns))


def order_by_clause(
    sort: str | None, allowed: dict[str, ColumnElement[Any]], default: str
) -> list[ColumnElement[Any]]:
    requested = sort or default
    descending = requested.startswith("-")
    column = allowed.get(requested.lstrip("-"))
    if column is None:
        raise BadRequestError(f"Cannot sort by that field. Allowed: {', '.join(sorted(allowed))}.")
    return [column.desc() if descending else column.asc()]


def paginate_rows(
    session: Session, statement: Select[Any], query: ListQuery
) -> tuple[Sequence[Row[Any]], int]:
    """Run a statement for one page and count all matches."""
    total = session.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    rows = session.execute(
        statement.offset((query.page - 1) * query.page_size).limit(query.page_size)
    ).all()
    return rows, total or 0


class Repository(Generic[ModelT]):
    def __init__(self, session: Session, model: type[ModelT]) -> None:
        self.session = session
        self.model = model

    @property
    def _archivable(self) -> bool:
        return hasattr(self.model, "archived_at")

    def select(self, *, include_archived: bool = False) -> Select[tuple[ModelT]]:
        statement = select(self.model)
        if self._archivable and not include_archived:
            statement = statement.where(self.model.archived_at.is_(None))
        return statement

    def get(self, record_id: uuid.UUID, *, include_archived: bool = False) -> ModelT | None:
        record = self.session.get(self.model, record_id)
        if record is None:
            return None
        if self._archivable and not include_archived and record.archived_at is not None:
            return None
        return record

    def paginate(
        self, statement: Select[tuple[ModelT]], query: ListQuery
    ) -> tuple[list[ModelT], int]:
        rows, total = paginate_rows(self.session, statement, query)
        return [row[0] for row in rows], total

    def add(self, record: ModelT) -> None:
        self.session.add(record)

    def delete(self, record: ModelT) -> None:
        self.session.delete(record)
