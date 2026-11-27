"""Correlations between risks and the risk appetite (all backends for the service parts)."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain import dependence
from riskmesh.domain.errors import ConflictError, ValidationError
from riskmesh.domain.risk_model import run_simulation
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository

LOGNORM = {"dist": "lognorm_p10_p90", "p10": 100_000, "p90": 2_000_000}


def two_risks(svc):
    hq = svc.create_business_unit("HQ")
    it = svc.create_business_unit("IT", parent_id=hq.id)
    a = svc.create_risk(it.id, "Ransomware", {"type": "poisson", "rate": 0.5}, LOGNORM)
    b = svc.create_risk(it.id, "Breach", {"type": "poisson", "rate": 0.5}, LOGNORM)
    return hq, it, a, b


# -- the engine ---------------------------------------------------------------------------------------

def _risks():
    spec = {"business_unit_id": "u", "frequency": {"type": "poisson", "rate": 3}, "impact": LOGNORM}
    return {"a": {"name": "A", **spec}, "b": {"name": "B", **spec}, "c": {"name": "C", **spec}}


def test_correlation_keeps_each_risk_and_raises_the_tail():
    units = {"u": {"name": "U"}}
    plain = run_simulation(_risks(), units, 20_000, seed=1)
    corr = run_simulation(_risks(), units, 20_000, seed=1, correlations=[("a", "b", 0.8)])
    for rid in "abc":  # same values per risk, only reordered
        assert np.array_equal(np.sort(plain.risk_losses[rid]), np.sort(corr.risk_losses[rid]))
    assert np.array_equal(plain.risk_losses["c"], corr.risk_losses["c"])  # not in any pair: untouched
    assert np.percentile(corr.total_losses(), 99) > np.percentile(plain.total_losses(), 99)
    achieved = corr.correlation["pairs"][0]["achieved"]
    assert 0.6 < achieved < 0.85 and corr.correlation["adjusted"] is False
    rep = corr.report()
    assert rep["total"]["mean"] == pytest.approx(plain.report()["total"]["mean"])
    assert rep["correlation"]["applied"] is True


def test_negative_correlation_and_repeatability():
    units = {"u": {"name": "U"}}
    a = run_simulation(_risks(), units, 10_000, seed=5, correlations=[("a", "b", -0.6)])
    b = run_simulation(_risks(), units, 10_000, seed=5, correlations=[("a", "b", -0.6)])
    assert np.array_equal(a.total_losses(), b.total_losses())
    assert a.correlation["pairs"][0]["achieved"] < -0.3


def test_inconsistent_pairs_are_adjusted():
    m, adjusted = dependence.correlation_matrix(["a", "b", "c"], [("a", "b", 0.9), ("b", "c", 0.9), ("a", "c", -0.9)])
    assert adjusted and np.all(np.linalg.eigvalsh(m) > 0) and np.allclose(np.diag(m), 1)


# -- the service ---------------------------------------------------------------------------------------

def test_correlation_crud_and_rules(svc):
    hq, it, a, b = two_risks(svc)
    c = svc.create_correlation(a.id, b.id, 0.5, rationale="same attackers")
    with pytest.raises(ConflictError):
        svc.create_correlation(b.id, a.id, 0.2)  # the same pair, either order
    with pytest.raises(ValidationError):
        svc.create_correlation(a.id, a.id, 0.2)
    with pytest.raises(ValidationError):
        svc.update_correlation(c.id, coefficient=1.5)
    assert svc.update_correlation(c.id, coefficient=-0.3).coefficient == -0.3
    assert [x.id for x in svc.list_correlations(risk_id=b.id)] == [c.id]
    svc.delete_risk(a.id)  # a correlation needs both risks
    assert svc.list_correlations() == []


def test_engines_report_correlation(svc):
    hq, it, a, b = two_risks(svc)
    svc.create_correlation(a.id, b.id, 0.7)
    analytic = svc.simulation_report(engine="analytic")
    assert analytic["correlation"]["applied"] is False and analytic["correlation"]["pair_count"] == 1
    mc = svc.simulation_report(5_000, 3, engine="monte_carlo")
    assert mc["correlation"]["applied"] is True


def test_appetite_rules(svc):
    hq, it, a, b = two_risks(svc)
    for bad in ([], [{"loss": 0, "probability": 0.1}], [{"loss": 1e6, "probability": 1.0}],
                [{"loss": 1e6, "probability": 0.01}, {"loss": 1e7, "probability": 0.1}],
                [{"loss": 1e6, "probability": 0.1}, {"loss": 1e6, "probability": 0.05}]):
        with pytest.raises(ValidationError):
            svc.create_appetite("Bad", bad)
    app = svc.create_appetite("Group", [{"loss": 1e7, "probability": 0.01}, {"loss": 1e6, "probability": 0.1}])
    assert [p["loss"] for p in app.points] == [1e6, 1e7]  # sorted


def test_appetite_is_checked_by_both_engines(svc):
    hq, it, a, b = two_risks(svc)
    svc.create_appetite("Tight", [{"loss": 500_000, "probability": 0.01}])
    svc.create_appetite("Loose", [{"loss": 50_000_000, "probability": 0.5}], business_unit_id=it.id)
    for report in (svc.simulation_report(engine="analytic"), svc.simulation_report(10_000, 1, engine="monte_carlo")):
        by_name = {x["name"]: x for x in report["appetite"]}
        assert by_name["Tight"]["breached"] and not by_name["Loose"]["breached"]
        assert by_name["Tight"]["points"][0]["actual"] > 0.1
        assert by_name["Loose"]["loss_exceedance_curve"]
    an = svc.simulation_report(engine="analytic")["appetite"][0]["points"][0]["actual"]
    mc = svc.simulation_report(50_000, 2, engine="monte_carlo")["appetite"][0]["points"][0]["actual"]
    assert an == pytest.approx(mc, abs=0.01)


def test_unit_report_only_has_appetites_inside_it(svc):
    hq, it, a, b = two_risks(svc)
    svc.create_appetite("Group", [{"loss": 1e6, "probability": 0.1}])
    svc.create_appetite("IT", [{"loss": 1e6, "probability": 0.1}], business_unit_id=it.id)
    assert [x["name"] for x in svc.simulation_report(unit_id=it.id)["appetite"]] == ["IT"]
    assert sorted(x["name"] for x in svc.simulation_report()["appetite"]) == ["Group", "IT"]


def test_unit_appetite_goes_with_the_unit(svc):
    hq, it, a, b = two_risks(svc)
    svc.create_appetite("IT", [{"loss": 1e6, "probability": 0.1}], business_unit_id=it.id)
    svc.delete_business_unit(it.id)
    assert svc.list_appetites() == []  # not turned into an organisation-wide appetite


def test_changes_make_runs_stale_and_old_fingerprints_stay(svc):
    hq, it, a, b = two_risks(svc)
    before = svc.analysis_fingerprint(svc._run_params("compare", "analytic", 0, None, True, None))
    run = svc.run_analysis()
    c = svc.create_correlation(a.id, b.id, 0.5)
    assert svc.get_run(run["run"]["id"])["run"]["stale"]
    svc.delete_correlation(c.id)
    after = svc.analysis_fingerprint(svc._run_params("compare", "analytic", 0, None, True, None))
    assert before == after  # nothing set: same inputs as before the feature existed


def test_excel_round_trip(svc):
    hq, it, a, b = two_risks(svc)
    svc.create_correlation(a.id, b.id, 0.45, rationale="shared platform")
    svc.create_appetite("IT", [{"loss": 1_000_000, "probability": 0.1}, {"loss": 5e6, "probability": 0.005}],
                        business_unit_id=it.id, statement="at most")
    result = svc.import_excel(svc.export_excel(), dry_run=True)
    assert result["changes"]["correlation"]["unchanged"] == 1 and result["changes"]["appetite"]["unchanged"] == 1


def test_excel_tolerance_text():
    from riskmesh.services.excel import _points
    assert _points("1.000.000 @ 10%; 5000000 @ 0,5%") == [{"loss": 1e6, "probability": 0.1},
                                                         {"loss": 5e6, "probability": 0.005}]
    assert _points("1000000 @ 0.1") == [{"loss": 1e6, "probability": 0.1}]
    with pytest.raises(ValueError):
        _points("1000000 10%")


def test_api():
    svc = RiskService(MemoryRepository())
    hq, it, a, b = two_risks(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.post("/api/correlations", json={"risk_a_id": a.id, "risk_b_id": b.id, "coefficient": 0.4})
    assert r.status_code == 201, r.text
    assert client.post("/api/correlations", json={"risk_a_id": a.id, "risk_b_id": b.id,
                                                  "coefficient": 1.2}).status_code == 422
    r = client.post("/api/appetites", json={"name": "Group", "points": [{"loss": 1e6, "probability": 0.2}]})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert client.patch(f"/api/appetites/{aid}", json={"statement": "ok"}).json()["statement"] == "ok"
    report = client.post("/api/simulations", json={"engine": "monte_carlo", "n_trials": 2000, "seed": 1}).json()
    assert report["appetite"][0]["name"] == "Group" and report["correlation"]["applied"]
    assert client.delete(f"/api/appetites/{aid}").status_code == 204
    assert len(client.get("/api/correlations").json()) == 1
