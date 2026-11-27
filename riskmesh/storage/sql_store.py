"""
Relational backend via SQLAlchemy (SQLite, PostgreSQL, ...).

Tables are generated from the domain dataclasses: str -> VARCHAR/TEXT,
int/float -> INTEGER/FLOAT, list/dict fields -> JSON columns.
No foreign keys on purpose: referential rules and cascades live in the
service layer, so every backend behaves identically.

Schema migrations: on start-up missing tables are created and missing
columns are added (new model fields are always nullable, so this is safe);
the schema version is kept in `riskmesh_meta`, and a database written by a
newer RiskMesh is refused instead of being damaged. Changes that are not
additive get a numbered function in MIGRATIONS.

Concurrency: a write transaction first updates the single row in
`riskmesh_lock`. That takes the database's write lock (SQLite) or a row lock
(PostgreSQL), so writers from any number of processes are serialised and
the audit chain stays linear. Readers are not blocked by it on PostgreSQL.
"""

import logging
import threading
import typing
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Type

from sqlalchemy import (JSON, Boolean, Column, Float, Index, Integer, MetaData, String, Table, Text, create_engine, delete,
                        inspect, insert, select, text, update)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from ..domain.models import ENTITY_TYPES, ApiToken, AuditEntry, Entity, User
from ..domain.audit import sha256 as audit_sha
from .base import E, Repository, StorageError

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Non-additive migrations: {version: function(connection)}, run in order when upgrading.
MIGRATIONS: Dict[int, Callable[[Connection], None]] = {}

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
    elif base is bool:
        col_type = Boolean
    elif base is float:
        col_type = Float
    elif base is int:
        col_type = Integer
    elif name in LONG_TEXT:
        col_type = Text
    else:
        col_type = String(255)
    indexed = name.endswith("_id") and origin not in (list, dict)
    # every column is nullable so that new fields can always be added to existing tables
    return Column(name, col_type, nullable=True, index=indexed)


def _table(cls: Type[Entity]) -> Table:
    hints = typing.get_type_hints(cls)
    return Table(cls.kind + "s", metadata, *[_column(n, hints[n]) for n in cls.field_names()])


TABLES: Dict[Type[Entity], Table] = {cls: _table(cls) for cls in ENTITY_TYPES}
AUDIT = TABLES[AuditEntry]
# one entry per sequence number: two writers can never both append entry n
Index("ux_audit_seq", AUDIT.c.seq, unique=True)
Index("ux_user_username", TABLES[User].c.username, unique=True)
Index("ux_token_hash", TABLES[ApiToken].c.token_hash, unique=True)
META = Table("riskmesh_meta", metadata, Column("key", String(64), primary_key=True), Column("value", String(255)))
LOCK = Table("riskmesh_lock", metadata, Column("id", Integer, primary_key=True), Column("n", Integer))

# The audit table is append-only: the database itself refuses UPDATE and DELETE.
_PROTECT = {
    "sqlite": [
        f"CREATE TRIGGER IF NOT EXISTS {AUDIT.name}_no_update BEFORE UPDATE ON {AUDIT.name} "
        "BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END",
        f"CREATE TRIGGER IF NOT EXISTS {AUDIT.name}_no_delete BEFORE DELETE ON {AUDIT.name} "
        "BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END",
    ],
    "postgresql": [
        "CREATE OR REPLACE FUNCTION riskmesh_audit_append_only() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'audit log is append-only'; END; $$ LANGUAGE plpgsql",
        f"DROP TRIGGER IF EXISTS {AUDIT.name}_append_only ON {AUDIT.name}",
        f"CREATE TRIGGER {AUDIT.name}_append_only BEFORE UPDATE OR DELETE ON {AUDIT.name} "
        "FOR EACH ROW EXECUTE FUNCTION riskmesh_audit_append_only()",
    ],
}


# Reporting views for Power BI, Excel etc. (read-only; columns are stable)
_FIGURES = ("risk_count, high_risk_count, eal_inherent, eal_residual, p50, p90, p99, tvar99, p99_inherent, "
            "score_inherent, score_residual, controls, events, actual_loss")
VIEWS = {
    "v_periods": "SELECT key AS period, period_type, start AS period_start, \"end\" AS period_end, status, "
                 "closed_at, closed_by, note, run_id, anchor_id FROM reporting_periods",
    "v_period_total": f"SELECT period_key AS period, period_end, {_FIGURES}, control_effectiveness "
                      "FROM period_facts WHERE level = 'total'",
    "v_period_unit": f"SELECT period_key AS period, period_end, entity_id AS unit_id, name AS unit, "
                     f"business_unit_id AS parent_unit_id, {_FIGURES} FROM period_facts WHERE level = 'unit'",
    "v_period_category": f"SELECT period_key AS period, period_end, entity_id AS category, {_FIGURES} "
                         "FROM period_facts WHERE level = 'category'",
    "v_period_risk": f"SELECT period_key AS period, period_end, entity_id AS risk_id, name AS risk, "
                     f"business_unit_id AS unit_id, category, likelihood_inherent, impact_inherent, "
                     f"likelihood_residual, impact_residual, {_FIGURES} FROM period_facts WHERE level = 'risk'",
}


class SqlRepository(Repository):

    def __init__(self, url: str = "sqlite:///riskmesh.db", echo: bool = False):
        kwargs: Dict[str, Any] = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}  # wait for another writer instead of failing at once
        try:
            self.engine = create_engine(url, **kwargs)
            self._local = threading.local()
            self._migrate()
        except OperationalError as e:
            raise StorageError(f"Cannot open the database {self.engine.url.render_as_string(hide_password=True)}: "
                               f"{e.orig}") from e

    # -- schema --------------------------------------------------------------

    def _migrate(self) -> None:
        with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                # several processes may start at once: migrate one at a time
                conn.execute(text("SELECT pg_advisory_xact_lock(727274)"))
            metadata.create_all(conn)
            row = conn.execute(select(META.c.value).where(META.c.key == "schema_version")).first()
            current = int(row[0]) if row else SCHEMA_VERSION
            if current > SCHEMA_VERSION:
                raise StorageError(f"The database uses schema version {current}, but this RiskMesh only knows "
                                   f"version {SCHEMA_VERSION}. Upgrade RiskMesh before using this database.")
            self._add_missing_columns(conn)
            for version in range(current + 1, SCHEMA_VERSION + 1):
                if version in MIGRATIONS:
                    log.info("Migrating database to schema version %s", version)
                    MIGRATIONS[version](conn)
            conn.execute(delete(META).where(META.c.key == "schema_version"))
            conn.execute(insert(META).values(key="schema_version", value=str(SCHEMA_VERSION)))
            if conn.execute(select(LOCK.c.id)).first() is None:
                conn.execute(insert(LOCK).values(id=1, n=0))
            self._protect_audit(conn)
            self._ensure_views(conn)

    def _ensure_views(self, conn: Connection) -> None:
        """(Re)creates the reporting views only when their definition changed."""
        version = audit_sha(VIEWS)
        row = conn.execute(select(META.c.value).where(META.c.key == "views_version")).first()
        existing = set(inspect(conn).get_view_names())
        if row and row[0] == version and existing >= set(VIEWS):
            return
        for name, sql in VIEWS.items():
            conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(text(f"CREATE VIEW {name} AS {sql}"))
        conn.execute(delete(META).where(META.c.key == "views_version"))
        conn.execute(insert(META).values(key="views_version", value=version))

    def _add_missing_columns(self, conn: Connection) -> None:
        inspector = inspect(conn)
        for table in TABLES.values():
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(conn.dialect)}'
                    log.info("Adding column %s.%s", table.name, col.name)
                    conn.execute(text(ddl))

    def _protect_audit(self, conn: Connection) -> None:
        for sql in _PROTECT.get(conn.dialect.name, []):
            conn.execute(text(sql))

    def schema_version(self) -> int:
        with self._read() as conn:
            row = conn.execute(select(META.c.value).where(META.c.key == "schema_version")).first()
        return int(row[0]) if row else 0

    # -- connections -----------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if getattr(self._local, "conn", None) is not None:
            yield  # re-entrant
            return
        with self.engine.begin() as conn:
            conn.execute(update(LOCK).where(LOCK.c.id == 1).values(n=LOCK.c.n + 1))  # take the write lock
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    @contextmanager
    def _read(self) -> Iterator[Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            yield conn
        else:
            with self.engine.connect() as c:
                yield c

    @contextmanager
    def _write(self) -> Iterator[Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            yield conn
        else:
            with self.engine.begin() as c:
                yield c

    # -- Repository API ----------------------------------------------------------

    def get(self, cls: Type[E], entity_id: str) -> Optional[E]:
        table = TABLES[cls]
        with self._read() as conn:
            row = conn.execute(select(table).where(table.c.id == entity_id)).mappings().first()
        return cls.from_dict(dict(row)) if row else None

    def list(self, cls: Type[E], **filters: Any) -> List[E]:
        table = TABLES[cls]
        stmt = select(table)
        for key, value in filters.items():
            stmt = stmt.where(table.c[key].is_(None) if value is None else table.c[key] == value)
        with self._read() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [cls.from_dict(dict(r)) for r in rows]

    def latest(self, cls: Type[E], field: str) -> Optional[E]:
        table = TABLES[cls]
        with self._read() as conn:
            row = conn.execute(select(table).order_by(table.c[field].desc()).limit(1)).mappings().first()
        return cls.from_dict(dict(row)) if row else None

    def save(self, entity: Entity) -> None:
        table = TABLES[type(entity)]
        with self._write() as conn:
            if table is not AUDIT:  # audit entries are only ever inserted
                conn.execute(delete(table).where(table.c.id == entity.id))
            conn.execute(insert(table).values(**entity.to_dict()))

    def delete(self, cls: Type[Entity], entity_id: str) -> bool:
        table = TABLES[cls]
        with self._write() as conn:
            return conn.execute(delete(table).where(table.c.id == entity_id)).rowcount > 0

    def clear(self) -> None:
        """Deletes everything. The append-only audit table can only be emptied by dropping it."""
        with self._write() as conn:
            for table in TABLES.values():
                if table is not AUDIT:
                    conn.execute(delete(table))
            AUDIT.drop(conn, checkfirst=True)
            AUDIT.create(conn)
            self._protect_audit(conn)

    def close(self) -> None:
        self.engine.dispose()
