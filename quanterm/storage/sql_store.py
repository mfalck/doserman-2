"""
Relational backend via SQLAlchemy (SQLite, PostgreSQL, SQL Server, ...).

Scalar fields become columns; list/dict fields (delegate_ids, asset_ids,
distribution specs, controls) are stored in JSON columns.
"""

from typing import Any, Dict, List, Optional, Type

from sqlalchemy import JSON, Column, MetaData, String, Table, Text, create_engine, delete, insert, select

from ..domain.models import Asset, BusinessUnit, Entity, Person, Risk
from .base import E, Repository

metadata = MetaData()

TABLES: Dict[Type[Entity], Table] = {
    Person: Table(
        "persons", metadata,
        Column("id", String(36), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("title", String(200)),
        Column("email", String(200)),
    ),
    BusinessUnit: Table(
        "business_units", metadata,
        Column("id", String(36), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("description", Text),
        Column("parent_id", String(36), index=True),
        Column("risk_owner_id", String(36)),
        Column("delegate_ids", JSON, nullable=False, default=list),
    ),
    Asset: Table(
        "assets", metadata,
        Column("id", String(36), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("asset_type", String(50), nullable=False),
        Column("business_unit_id", String(36), index=True, nullable=False),
        Column("description", Text),
        Column("criticality", String(20)),
    ),
    Risk: Table(
        "risks", metadata,
        Column("id", String(36), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("business_unit_id", String(36), index=True, nullable=False),
        Column("frequency", JSON, nullable=False),
        Column("impact", JSON, nullable=False),
        Column("description", Text),
        Column("category", String(50)),
        Column("asset_ids", JSON, nullable=False, default=list),
        Column("owner_id", String(36)),
        Column("residual_frequency", JSON),
        Column("residual_impact", JSON),
        Column("controls", JSON, nullable=False, default=list),
    ),
}
# No foreign keys on purpose: referential rules and cascades live in the
# service layer, so every backend behaves identically.


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
