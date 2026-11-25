"""Stored analysis runs: reuse while data is unchanged, register, labels, reproduction."""

from riskmesh.domain import analytic
from riskmesh.domain.models import AnalysisRun, AuditEntry

FREQ = {"type": "poisson", "rate": 1.0}
IMPACT = {"dist": "pert", "min": 1_000, "mode": 5_000, "max": 20_000}


def small_model(svc):
    a = svc.create_business_unit("A")
    b = svc.create_business_unit("B")
    ra = svc.create_risk(a.id, "Risk A", FREQ, IMPACT)
    rb = svc.create_risk(b.id, "Risk B", FREQ, IMPACT)
    return a, b, ra, rb


def test_second_request_reuses_the_stored_run(svc):
    small_model(svc)
    first = svc.run_analysis()
    second = svc.run_analysis()
    assert first["run"]["cached"] is False and second["run"]["cached"] is True
    assert first["run"]["id"] == second["run"]["id"] and first["run"]["number"] == 1
    assert {k: v for k, v in first.items() if k != "run"} == {k: v for k, v in second.items() if k != "run"}
    assert len(svc.repo.list(AnalysisRun)) == 1


def test_only_relevant_changes_invalidate(svc):
    _, _, ra, _ = small_model(svc)
    run1 = svc.run_analysis()["run"]
    svc.update_risk(ra.id, description="wording only")        # not used by the analysis
    assert svc.run_analysis()["run"]["id"] == run1["id"]
    svc.update_risk(ra.id, frequency={"type": "poisson", "rate": 2.0})
    run2 = svc.run_analysis()["run"]
    assert run2["cached"] is False and run2["number"] == 2
    stale = {r["number"]: r["stale"] for r in svc.list_runs()}
    assert stale == {2: False, 1: True}


def test_settings_are_part_of_the_fingerprint(svc):
    small_model(svc)
    runs = {svc.run_analysis(mode=m, engine=e, n_trials=2_000, seed=1)["run"]["id"]
            for m in ("single", "compare") for e in ("analytic", "monte_carlo")}
    assert len(runs) == 4
    assert svc.run_analysis(mode="single", engine="monte_carlo", n_trials=3_000, seed=1)["run"]["cached"] is False


def test_unit_scope_ignores_changes_elsewhere(svc):
    a, _, _, rb = small_model(svc)
    run = svc.run_analysis(unit_id=a.id)["run"]
    svc.update_risk(rb.id, impact={"dist": "fixed", "value": 1})  # other unit
    assert svc.run_analysis(unit_id=a.id)["run"]["id"] == run["id"]


def test_monte_carlo_without_seed_is_never_reused(svc):
    small_model(svc)
    r1 = svc.run_analysis(mode="single", engine="monte_carlo", n_trials=1_000, seed=None)
    r2 = svc.run_analysis(mode="single", engine="monte_carlo", n_trials=1_000, seed=None)
    assert not r1["run"]["cached"] and not r2["run"]["cached"] and r1["run"]["id"] != r2["run"]["id"]


def test_engine_version_change_forces_recalculation(svc, monkeypatch):
    small_model(svc)
    svc.run_analysis()
    monkeypatch.setattr(analytic, "ENGINE_VERSION", "analytic-test")
    assert svc.run_analysis()["run"]["cached"] is False


def test_labels_audit_and_reproduction(svc):
    _, _, ra, _ = small_model(svc)
    with svc.context(actor="alice"):
        run = svc.run_analysis(label="Board meeting")["run"]
    assert run["label"] == "Board meeting" and run["created_by"] == "alice"
    assert run["audit_seq"] == max(e.seq for e in svc.repo.list(AuditEntry) if e.action != "RUN")
    svc.label_run(run["id"], "Board meeting 1 Dec")
    actions = [e.action for e in sorted(svc.repo.list(AuditEntry), key=lambda e: e.seq)][-2:]
    assert actions == ["RUN", "LABEL"]
    assert svc.verify_audit()["ok"]

    assert svc.reproduce_run(run["id"])["reproducible"] is True
    svc.update_risk(ra.id, impact={"dist": "fixed", "value": 9_999})
    result = svc.reproduce_run(run["id"])
    assert result["reproducible"] is None and result["run"]["stale"] is True
    assert svc.get_run(run["id"])["run"]["label"] == "Board meeting 1 Dec"
