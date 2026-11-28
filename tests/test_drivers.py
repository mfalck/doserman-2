"""Drivers: common causes behind risks (all backends for the service parts)."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain.errors import ConflictError, ValidationError
from riskmesh.domain.risk_model import driver_multipliers, run_simulation
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository

LOGNORM = {"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 500_000}


def build(svc):
    it = svc.create_business_unit("IT")
    a = svc.create_risk(it.id, "A", {"type": "poisson", "rate": 1.0}, LOGNORM)
    b = svc.create_risk(it.id, "B", {"type": "poisson", "rate": 1.0}, LOGNORM)
    c = svc.create_risk(it.id, "C", {"type": "poisson", "rate": 1.0}, LOGNORM)
    d = svc.create_driver("Hygiene", uncertainty=2.0)
    svc.create_driver_effect(d.id, a.id, frequency_sensitivity=1.0)
    svc.create_driver_effect(d.id, b.id, frequency_sensitivity=2.0, impact_sensitivity=0.5)
    return dict(it=it, a=a, b=b, c=c, d=d)


def test_multipliers_average_one_and_move_together():
    z = np.random.default_rng(1).standard_normal(200_000)
    fm, im = driver_multipliers([{"id": "d", "sigma": 0.5, "f": 2.0, "i": 0.0}], {"d": z}, 200_000)
    assert fm.mean() == pytest.approx(1.0, rel=0.02) and np.all(im == 1.0)


def test_drivers_add_tail_but_keep_the_mean():
    units = {"u": {"name": "U"}}
    spec = {"business_unit_id": "u", "frequency": {"type": "poisson", "rate": 2.0}, "impact": LOGNORM}
    plain = {k: {"name": k, **spec} for k in "ab"}
    driven = {k: {"name": k, **spec, "drivers": [{"id": "d", "level": 1.0, "sigma": 0.6, "f": 1.0, "i": 0.5}]}
              for k in "ab"}
    p = run_simulation(plain, units, 50_000, seed=3).total_losses()
    q = run_simulation(driven, units, 50_000, seed=3).total_losses()
    assert q.mean() == pytest.approx(p.mean(), rel=0.03)
    assert np.percentile(q, 99) > np.percentile(p, 99) * 1.1
    r = run_simulation(driven, units, 50_000, seed=3).risk_losses
    assert np.corrcoef(r["a"], r["b"])[0, 1] > 0.2


def test_level_scales_the_estimates(svc):
    x = build(svc)
    base = svc.simulation_report()["total"]["mean"]
    svc.update_driver(x["d"].id, level=0.5)
    # A: rate x0.5; B: rate x0.25 and loss x0.5**0.5; C unchanged
    scores = {r.name: svc.risk_scores(r)["residual"]["expected_annual_loss"] for r in svc.list_risks()}
    single = scores["C"]
    assert scores["A"] == pytest.approx(single * 0.5, rel=1e-6)
    assert scores["B"] == pytest.approx(single * 0.25 * 0.5 ** 0.5, rel=1e-3)
    assert svc.simulation_report()["total"]["mean"] < base
    assert svc.get_risk(x["a"].id).frequency == {"type": "poisson", "rate": 1.0}  # stored estimate untouched


def test_reports_say_whether_variation_is_applied(svc):
    build(svc)
    assert svc.simulation_report(engine="analytic")["drivers"]["variation_applied"] is False
    assert svc.simulation_report(5_000, 1, engine="monte_carlo")["drivers"]["variation_applied"] is True


def test_overview_and_what_if(svc):
    x = build(svc)
    o = svc.driver_overview()
    d = o["drivers"][0]
    assert {e["risk"] for e in d["effects"]} == {"A", "B"} and o["risks_without_driver"] == ["C"]
    assert d["high"]["mean"] > o["base"]["mean"] > d["low"]["mean"] and d["swing_p99"] > 0
    w = svc.driver_what_if({x["d"].id: 0.5})
    assert {r["name"] for r in w["risks"]} == {"A", "B"} and w["after"]["mean"] < w["before"]["mean"]
    assert svc.get_driver(x["d"].id).level == 1.0  # what-if changes nothing
    with pytest.raises(ValidationError):
        svc.driver_what_if({x["d"].id: 0})


def test_validation_uniqueness_and_cascade(svc):
    x = build(svc)
    with pytest.raises(ValidationError):
        svc.create_driver("Bad", uncertainty=0.5)
    with pytest.raises(ValidationError):
        svc.create_driver_effect(x["d"].id, x["c"].id, frequency_sensitivity=0, impact_sensitivity=0)
    with pytest.raises(ConflictError):
        svc.create_driver_effect(x["d"].id, x["a"].id)
    svc.delete_risk(x["a"].id)
    assert len(svc.list_driver_effects()) == 1
    svc.delete_driver(x["d"].id)
    assert svc.list_driver_effects() == []


def test_level_changes_make_runs_stale(svc):
    x = build(svc)
    run = svc.run_analysis()
    svc.update_driver(x["d"].id, level=1.2)
    assert svc.get_run(run["run"]["id"])["run"]["stale"]


def test_excel_round_trip(svc):
    build(svc)
    preview = svc.import_excel(svc.export_excel(), dry_run=True)
    assert preview["changes"]["driver"]["unchanged"] == 1 and preview["changes"]["driver_effect"]["unchanged"] == 2
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel())
    assert empty.list_drivers()[0].uncertainty == 2.0 and len(empty.list_driver_effects()) == 2


def test_api():
    svc = RiskService(MemoryRepository())
    x = build(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.post("/api/drivers", json={"name": "Economy", "uncertainty": 1.5})
    assert r.status_code == 201, r.text
    r = client.post("/api/driver-effects", json={"driver_id": r.json()["id"], "risk_id": x["c"].id})
    assert r.status_code == 201, r.text
    assert len(client.get("/api/drivers/overview").json()["drivers"]) == 2
    w = client.post("/api/drivers/what-if", json={"levels": {x["d"].id: 0.7}}).json()
    assert w["after"]["mean"] < w["before"]["mean"]
    assert client.patch(f"/api/drivers/{x['d'].id}", json={"level": 0}).status_code == 422
