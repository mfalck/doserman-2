"""P80, the negative binomial, ISO 27005 on Poisson and NIS2 significance."""

import math

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain import analytic
from riskmesh.domain import risk_model as rm
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.services.significance import per_year
from riskmesh.storage import MemoryRepository

LOGNORM = {"dist": "lognorm_p10_p90", "p10": 10_000, "p90": 200_000}


def test_reports_have_p80():
    risks = {"a": {"name": "A", "business_unit_id": "u", "frequency": {"type": "poisson", "rate": 2}, "impact": LOGNORM}}
    an = analytic.analyse(risks, {"u": {"name": "U"}})["total"]
    mc = rm.run_simulation(risks, {"u": {"name": "U"}}, 20_000, seed=1).report()["total"]
    assert an["p50"] <= an["p80"] <= an["p90"] and "p80" in mc


def test_negbin_from_a_range_and_back():
    s = rm.negbin_from_range(2.0, 4.0)
    assert s["type"] == "negbin" and rm.rate_p90_of(s) == pytest.approx(4.0, rel=1e-4)
    assert rm.negbin_from_range(2.0, 2.0) == {"type": "poisson", "rate": 2.0}
    assert rm.negbin_from_range(2.0, 50.0)["shape"] == pytest.approx(0.45)  # capped at the widest
    assert rm.annual_event_probability(s) < rm.annual_event_probability({"type": "poisson", "rate": 2.0})


def test_negbin_engines_agree_and_keep_the_mean():
    units = {"u": {"name": "U"}}
    nb = {"a": {"name": "A", "business_unit_id": "u", "frequency": rm.negbin_from_range(2.0, 4.0), "impact": LOGNORM}}
    po = {"a": {**nb["a"], "frequency": {"type": "poisson", "rate": 2.0}}}
    an, an_p = analytic.analyse(nb, units)["total"], analytic.analyse(po, units)["total"]
    mc = rm.run_simulation(nb, units, 100_000, seed=2).report()["total"]
    assert an["mean"] == pytest.approx(an_p["mean"], rel=1e-3)          # same expected loss
    assert an["p99"] > an_p["p99"]                                       # heavier tail
    for key in ("mean", "p80", "p99"):
        assert mc[key] == pytest.approx(an[key], rel=0.06), key


def test_negbin_given_as_a_range_on_a_risk(svc):
    it = svc.create_business_unit("IT")
    r = svc.create_risk(it.id, "R", {"type": "negbin", "rate": 2, "rate_p90": 4}, LOGNORM)
    assert r.frequency["shape"] > 0 and r.frequency["rate_p90"] == pytest.approx(4)
    with pytest.raises(ValidationError):
        svc.create_risk(it.id, "Bad", {"type": "negbin", "rate": 2, "rate_p90": 1}, LOGNORM)
    # drivers scale the whole rate: the shape stays, the high estimate scales along
    d = svc.create_driver("D", level=2.0)
    svc.create_driver_effect(d.id, r.id)
    e = svc.engine_risk(svc.get_risk(r.id))
    assert e["frequency"]["rate"] == pytest.approx(4) and e["frequency"]["rate_p90"] == pytest.approx(8)


def test_excel_high_estimate_round_trip(svc):
    it = svc.create_business_unit("IT")
    svc.create_risk(it.id, "R", {"type": "negbin", "rate": 2, "rate_p90": 4}, LOGNORM)
    assert svc.import_excel(svc.export_excel(), dry_run=True)["changes"]["risk"]["unchanged"] == 1
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel())
    f = empty.list_risks()[0].frequency
    assert f["type"] == "negbin" and f["rate_p90"] == pytest.approx(4, rel=1e-3)


def test_iso27005_on_poisson_by_default_and_v1_kept(svc):
    it = svc.create_business_unit("IT")
    r = svc.create_risk(it.id, "R", method="iso27005", method_inputs={"likelihood": 5, "consequence": 2})
    assert r.frequency["type"] == "poisson" and r.frequency["rate"] == pytest.approx(-math.log(0.1), rel=1e-4)
    assert svc.risk_scores(r)["inherent"]["likelihood"] == 5
    old = svc.create_risk(it.id, "Old", method="iso27005",
                          method_inputs={"likelihood": 3, "consequence": 3, "method_version": "1"})
    assert old.frequency["type"] == "bernoulli" and old.method_inputs["frequency_model"] == "bernoulli"


def test_significance_per_year_formulas():
    assert per_year({"type": "poisson", "rate": 2.0}, 0.1) == pytest.approx((0.2, 1 - math.exp(-0.2)))
    assert per_year({"type": "bernoulli", "p": 0.5}, 0.2) == pytest.approx((0.1, 0.1))
    m, p = per_year({"type": "negbin", "rate": 2.0, "shape": 1.0}, 0.5)
    assert m == pytest.approx(1.0) and p == pytest.approx(1 - 0.5)


def test_significance_report(svc):
    it = svc.create_business_unit("IT")
    svc.create_risk(it.id, "Big", {"type": "poisson", "rate": 1.0}, {"dist": "uniform", "min": 0, "max": 2_000_000})
    svc.create_risk(it.id, "Small", {"type": "poisson", "rate": 5.0}, {"dist": "fixed", "value": 1_000})
    svc.create_risk(it.id, "Outage", {"type": "poisson", "rate": 1.0}, {"dist": "fixed", "value": 1_000},
                    outage_hours={"dist": "uniform", "min": 0, "max": 48})
    r = svc.significance_report(loss=1_000_000, hours=24)
    by = {x["name"]: x for x in r["risks"]}
    assert by["Big"]["share_of_events"] == pytest.approx(0.5, abs=0.02) and "Small" not in by
    assert by["Outage"]["share_of_events"] == pytest.approx(0.5, abs=0.02)
    assert r["total"]["expected_per_year"] == pytest.approx(1.0, abs=0.05)
    assert svc.significance_report(loss=1_000_000)["risks"][0]["name"] == "Big"  # no hour threshold


def test_calibration_can_keep_the_rate_uncertainty(svc):
    it = svc.create_business_unit("IT")
    r = svc.create_risk(it.id, "R", {"type": "poisson", "rate": 1.0}, LOGNORM, evidence_categories=["x"])
    for d in ("2025-01-01", "2025-06-01", "2026-01-01"):
        svc.create_observation(d, "x", count=2, source="log", business_unit_id=it.id)
    r = svc.apply_calibration(r.id, impact=False, uncertain=True)
    assert r.frequency["type"] == "negbin" and r.frequency["rate_p90"] > r.frequency["rate"]


def test_api():
    svc = RiskService(MemoryRepository())
    it = svc.create_business_unit("IT")
    svc.create_risk(it.id, "Big", {"type": "poisson", "rate": 1.0}, {"dist": "fixed", "value": 5_000_000})
    svc.significant_loss = 3_750_000
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.get("/api/significance").json()
    assert r["threshold"]["loss"] == 3_750_000 and r["total"]["expected_per_year"] == pytest.approx(1.0)
    assert client.get("/api/significance", params={"loss": 10_000_000}).json()["risks"] == []
