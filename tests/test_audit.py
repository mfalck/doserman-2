"""Tamper-evident audit log: every test runs on every backend."""

import copy

import pytest

from riskmesh.domain import audit
from riskmesh.domain.models import AuditEntry, Person, Risk
from riskmesh.services import RiskService
from riskmesh.services.demo import seed_demo
from riskmesh.storage.json_store import MemoryRepository


def is_sql(repo):
    return type(repo).__name__ == "SqlRepository"


def entries(svc):
    return sorted(svc.repo.list(AuditEntry), key=lambda e: e.seq)


def test_chain_is_valid_after_normal_use(svc):
    seed_demo(svc)
    risk = svc.list_risks()[0]
    svc.update_risk(risk.id, description="updated")
    svc.delete_risk(risk.id)
    result = svc.verify_audit()
    assert result["ok"], result
    log = entries(svc)
    assert [e.seq for e in log] == list(range(1, len(log) + 1))
    assert log[0].prev_hash == audit.GENESIS
    assert all(b.prev_hash == a.hash for a, b in zip(log, log[1:]))
    assert result["head_hash"] == log[-1].hash


def test_field_level_changes_reason_and_source(svc):
    bu = svc.create_business_unit("Ops")
    r = svc.create_risk(bu.id, "Outage", {"type": "poisson", "rate": 1}, {"dist": "fixed", "value": 100})
    with svc.context(actor="alice", source="api", reason="annual review"):
        svc.update_risk(r.id, frequency={"type": "poisson", "rate": 2}, category="it")
    e = svc.list_audit(limit=1)[0]
    assert (e.user_id, e.source, e.reason) == ("alice", "api", "annual review")
    assert e.changes == {"frequency": [{"type": "poisson", "rate": 1}, {"type": "poisson", "rate": 2}],
                         "category": [None, "it"]}
    assert svc.actor == "system" and svc.reason is None  # context restored
    created = svc.list_audit(entity_id=r.id)[-1]
    assert created.action == "CREATE" and created.changes["name"] == [None, "Outage"]


def test_delete_records_the_removed_state(svc):
    p = svc.create_person("Bob", title="CFO")
    svc.delete_person(p.id)
    e = svc.list_audit(limit=1)[0]
    assert e.action == "DELETE" and e.state_hash is None
    assert e.changes["title"] == ["CFO", None]


def test_altered_entry_is_detected_or_blocked(svc):
    svc.create_person("Bob")
    svc.create_person("Eve")
    target = copy.deepcopy(entries(svc)[0])
    target.details = "Mallory"
    if is_sql(svc.repo):
        with pytest.raises(Exception):  # append-only: insert of an existing id fails
            svc.repo.save(target)
        with svc.repo.engine.begin() as conn, pytest.raises(Exception):
            from sqlalchemy import text
            conn.execute(text("UPDATE audit_entrys SET details = 'Mallory' WHERE seq = 1"))
    else:
        svc.repo.save(target)
        errors = svc.verify_audit()["chain_errors"]
        assert any(err["seq"] == 1 and "altered" in err["error"] for err in errors)


def test_removed_entry_is_detected_or_blocked(svc):
    for name in ("A", "B", "C"):
        svc.create_person(name)
    middle = entries(svc)[1]
    if is_sql(svc.repo):
        with pytest.raises(Exception):
            svc.repo.delete(AuditEntry, middle.id)
        assert svc.verify_audit()["ok"]
    else:
        svc.repo.delete(AuditEntry, middle.id)
        result = svc.verify_audit(check_data=False)
        assert not result["ok"]
        assert any("missing" in e["error"] or "prev_hash" in e["error"] for e in result["chain_errors"])


def test_data_changed_outside_riskmesh_is_detected(svc):
    bu = svc.create_business_unit("Ops")
    r = svc.create_risk(bu.id, "Outage", {"type": "poisson", "rate": 1}, {"dist": "fixed", "value": 100})
    p = svc.create_person("Bob")
    assert svc.verify_audit()["ok"]

    tampered = svc.repo.get(Risk, r.id)
    tampered.impact = {"dist": "fixed", "value": 1}       # edited directly in the database
    svc.repo.save(tampered)
    svc.repo.delete(Person, p.id)                         # deleted directly
    svc.repo.save(Person(name="Ghost"))                   # inserted directly

    errors = {(e["entity_id"], e["error"].split(" ")[0]) for e in svc.verify_audit()["data_errors"]}
    assert (r.id, "changed") in errors
    assert (p.id, "missing") in errors
    assert any(kind == "not" for _, kind in errors)       # "not in the audit log"


def test_chain_continues_in_a_new_service_instance(svc):
    svc.create_person("A")
    again = RiskService(svc.repo)                          # e.g. after a restart
    again.create_person("B")
    assert again.verify_audit()["ok"]
    assert [e.seq for e in entries(again)] == [1, 2]


def test_import_writes_one_entry_per_entity(svc):
    seed_demo(svc)
    data = svc.export_data()
    svc.reset_all()
    with svc.context(source="import:test.json:sha256:abc"):
        svc.import_data(data)
    log = entries(svc)
    n_entities = sum(len(v) for k, v in data.items() if k != "version")
    creates = [e for e in log if e.action == "CREATE"]
    assert len(creates) == n_entities
    assert all(e.source == "import:test.json:sha256:abc" for e in log)
    assert log[-1].action == "IMPORT"
    assert svc.verify_audit()["ok"]

    # importing the same file again changes nothing, so it adds no entity entries
    before = len(log)
    svc.import_data(data, replace=False)
    assert len(entries(svc)) == before + 1  # only the IMPORT summary
    assert svc.verify_audit()["ok"]


def test_replace_import_records_deletions(svc):
    svc.create_person("Old")
    keep = svc.create_person("Keep")
    svc.import_data({"persons": [keep.to_dict()]}, replace=True)
    deleted = [e for e in entries(svc) if e.action == "DELETE"]
    assert [e.changes["name"][0] for e in deleted] == ["Old"]
    assert svc.verify_audit()["ok"]


def test_reset_starts_a_new_chain(svc):
    svc.create_person("A")
    svc.reset_all()
    svc.create_person("B")
    log = entries(svc)
    assert [e.seq for e in log] == [1] and log[0].prev_hash == audit.GENESIS
    assert svc.verify_audit()["ok"]


def test_hash_ignores_int_float_representation():
    assert audit.sha256({"v": 400000}) == audit.sha256({"v": 400000.0})
    assert audit.sha256({"v": 0.5}) != audit.sha256({"v": 0.6})


def test_verify_on_empty_log():
    result = RiskService(MemoryRepository()).verify_audit()
    assert result["ok"] and result["entries"] == 0 and result["head_hash"] == audit.GENESIS
