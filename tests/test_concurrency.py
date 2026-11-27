"""Transactions, write locking across processes and threads, and schema migration."""

import multiprocessing as mp
import os
import threading

import pytest
from sqlalchemy import create_engine, text

from riskmesh.domain import audit
from riskmesh.domain.models import AuditEntry, Person, Risk
from riskmesh.services import RiskService
from riskmesh.storage import JsonRepository
from riskmesh.storage.base import StorageError
from riskmesh.storage.sql_store import SCHEMA_VERSION, SqlRepository


def _writer(url: str, worker: int, n: int) -> None:
    """Runs in a separate process: its own repository and service on the same database."""
    svc = RiskService(SqlRepository(url), actor=f"worker{worker}")
    for i in range(n):
        svc.create_person(f"p{worker}-{i}")
    svc.repo.close()


def _database_urls():
    urls = ["sqlite"]
    if os.environ.get("RISKMESH_TEST_POSTGRES_URL"):
        urls.append(os.environ["RISKMESH_TEST_POSTGRES_URL"])
    return urls


@pytest.mark.parametrize("db", _database_urls())
def test_processes_writing_to_one_database_keep_one_linear_chain(tmp_path, db):
    url = f"sqlite:///{(tmp_path / 'shared.db').as_posix()}" if db == "sqlite" else db
    first = SqlRepository(url)  # create schema first
    first.clear()
    first.close()
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(url, w, 15)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    svc = RiskService(SqlRepository(url))
    assert len(svc.repo.list(Person)) == 60
    assert [e.seq for e in sorted(svc.repo.list(AuditEntry), key=lambda e: e.seq)] == list(range(1, 61))
    assert svc.verify_audit()["ok"]


def test_threads_with_separate_services_share_one_chain(tmp_path):
    repo = SqlRepository(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    errors = []

    def work(n):
        try:
            svc = RiskService(repo, actor=f"t{n}")
            for i in range(10):
                svc.create_person(f"{n}-{i}")
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert RiskService(repo).verify_audit()["ok"] and len(repo.list(AuditEntry)) == 40


def test_failed_operation_leaves_nothing_behind(svc, monkeypatch):
    svc.create_person("Before")

    def broken_audit(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(svc, "_audit", broken_audit)
    with pytest.raises(RuntimeError):
        svc.create_person("Half")
    monkeypatch.undo()
    assert [p.name for p in svc.repo.list(Person)] == ["Before"]
    svc.create_person("After")  # the chain continues correctly after the rollback
    assert svc.verify_audit()["ok"]


def test_json_file_is_locked_by_one_process(tmp_path):
    path = str(tmp_path / "data.json")
    first = JsonRepository(path)
    with pytest.raises(StorageError, match="in use"):
        JsonRepository(path)
    first.close()
    JsonRepository(path).close()


def test_missing_columns_are_added_and_newer_schema_refused(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as conn:  # a "version 0" persons table without the email column
        conn.execute(text("CREATE TABLE persons (id VARCHAR(64) PRIMARY KEY, name VARCHAR(255), title VARCHAR(255))"))
        conn.execute(text("INSERT INTO persons (id, name) VALUES ('p1', 'Old')"))
    repo = SqlRepository(url)
    assert repo.get(Person, "p1").email is None
    p = repo.get(Person, "p1")
    p.email = "x@example.com"
    repo.save(p)
    assert repo.get(Person, "p1").email == "x@example.com"
    assert repo.schema_version() == SCHEMA_VERSION
    repo.close()

    with engine.begin() as conn:
        conn.execute(text("UPDATE riskmesh_meta SET value = :v WHERE key = 'schema_version'"),
                     {"v": str(SCHEMA_VERSION + 1)})
    with pytest.raises(StorageError, match="newer|Upgrade"):
        SqlRepository(url)
    engine.dispose()


def test_old_records_get_defaults_for_new_fields():
    r = Risk.from_dict({"id": "r", "name": "n", "business_unit_id": "u", "frequency": {}, "impact": {},
                        "asset_ids": None, "unknown_future_field": 1})
    assert r.asset_ids == [] and r.owner_id is None


def test_state_hash_ignores_new_default_fields_and_accepts_legacy(svc):
    p = svc.create_person("Ann")
    entry = svc.list_audit(limit=1)[0]
    assert entry.state_hash == audit.state_hash(svc.repo.get(Person, p.id))
    # an entry written by an older version stored the full-dict hash: still accepted
    assert audit.legacy_state_hash(svc.repo.get(Person, p.id)) != entry.state_hash
