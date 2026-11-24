"""
Relational backend via SQLAlchemy (SQLite, PostgreSQL, SQL Server, ...).

Tables are generated from the domain dataclasses: str -> VARCHAR/TEXT,
int/float -> INTEGER/FLOAT, list/dict fields -> JSON columns.
No foreign keys on purpose: referential rules and cascades live in the
service layer, so every backend behaves identically.

Note: `create_all` creates missing tables but does not add new columns to
existing ones. After a model change, migrate with export -> new DB -> import.
"""

import typing
from typing import Any, Dict, List, Optional, Type

from sqlalchemy import (JSON, Column, Float, Integer, MetaData, String, Table, Text, create_engine, delete, insert,
                        select)

from ..domain.models import ENTITY_TYPES, Entity
from .base import E, Repository

metadata = MetaData()
LONG_TEXT = {"description", "details", "impact_description"}


def _column(name: str, hint: Any) -> Column:
    optional = type(None) in typing.get_args(hint)
    base = next((a for a in typing.get_args(hint) if a is not type(None)), hint) if optional else hint
    origin = typing.get_origin(base) or base
    if name == "id":
        return Column("id", String(64), primary_key=True)
    if origin in (list, dict, List, Dict):
        col_type = JSON
    elif base is float:
        col_type = Float
    elif base is int:
        col_type = Integer
    elif name in LONG_TEXT:
        col_type = Text
    else:
        col_type = String(255)
    indexed = name.endswith("_id") and origin not in (list, dict)
    return Column(name, col_type, nullable=optional or origin in (list, dict), index=indexed)


def _table(cls: Type[Entity]) -> Table:
    hints = typing.get_type_hints(cls)
    return Table(cls.kind + "s", metadata, *[_column(n, hints[n]) for n in cls.field_names()])


TABLES: Dict[Type[Entity], Table] = {cls: _table(cls) for cls in ENTITY_TYPES}


class SqlRepository(Repository):

    def __init__(self, url: str = "sqlite:///quanterm.db", echo: bool = False):
        self.engine = create_engine(url, echo=echo, future=True)
        metadata.create_all(self.engine)

    def get(self, cls: Type[E], entity_id: str) -> Optional[E]:
        table = TABLES[cls]
        with self.engine.connect() as conn:
            row = conn.execute(select(table).where(table.c.id == entity_id)).mappings().first()
        return cls.from_dict(dict(row)) if row else None

    def list(self, cls: Type[E], **filters: Any) -> List[E]:
        table = TABLES[cls]
        stmt = select(table)
        for key, value in filters.items():
            stmt = stmt.where(table.c[key].is_(None) if value is None else table.c[key] == value)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [cls.from_dict(dict(r)) for r in rows]

    def save(self, entity: Entity) -> None:
        table = TABLES[type(entity)]
        with self.engine.begin() as conn:
            conn.execute(delete(table).where(table.c.id == entity.id))
            conn.execute(insert(table).values(**entity.to_dict()))

    def delete(self, cls: Type[Entity], entity_id: str) -> bool:
        table = TABLES[cls]
        with self.engine.begin() as conn:
            return conn.execute(delete(table).where(table.c.id == entity_id)).rowcount > 0

    def clear(self) -> None:
        with self.engine.begin() as conn:
            for table in TABLES.values():
                conn.execute(delete(table))

    def close(self) -> None:
        self.engine.dispose()
