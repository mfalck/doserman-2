"""Complete backups: restore with history intact, also on another backend."""

import copy

import pytest

from riskmesh.domain.errors import ConflictError, ValidationError
from riskmesh.services import RiskService
from riskmesh.services.demo import seed_demo
from riskmesh.storage import MemoryRepository


def test_backup_restores_everything_on_every_backend(svc):
    source = RiskService(MemoryRepository())
    seed_demo(source)
    source.create_user("admin", "admin", "correct horse battery")
    run = source.run_analysis(label="Q4")["run"]
    backup = source.backup_data()

    result = svc.restore_data(backup)
    assert result["verification"]["ok"], result["verification"]
    assert result["restored"]["audit_entry"] == backup["audit_head_seq"]
    assert svc.authenticate("admin", "correct horse battery")
    restored_run = svc.list_runs()[0]
    assert restored_run["label"] == "Q4" and restored_run["stale"] is False and restored_run["id"] == run["id"]
    svc.create_person("after restore")  # the chain continues on the new backend
    assert svc.verify_audit()["ok"]


def test_restore_refuses_damaged_files_and_non_empty_targets(svc):
    source = RiskService(MemoryRepository())
    source.create_person("A")
    backup = source.backup_data()
    tampered = copy.deepcopy(backup)
    tampered["content"]["person"][0]["name"] = "B"
    with pytest.raises(ValidationError, match="checksum"):
        svc.restore_data(tampered)
    svc.create_person("existing")
    with pytest.raises(ConflictError):
        svc.restore_data(backup)
    assert svc.restore_data(backup, force=True)["verification"]["ok"]
    assert [p.name for p in svc.list_persons()] == ["A"]
