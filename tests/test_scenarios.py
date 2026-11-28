"""Scenarios: named what-if cases compared with today (all backends for the service parts)."""

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository

FIXED = {"dist": "fixed", "value": 100_000}


def build(svc):
    it = svc.create_business_unit("IT")
    a = svc.create_risk(it.id, "A", {"type": "poisson", "rate": 1.0}, FIXED)
    b = svc.create_risk(it.id, "B", {"type": "poisson", "rate": 1.0}, FIXED)
    d = svc.create_driver("Economy")
    svc.create_driver_effect(d.id, a.id, frequency_sensitivity=1.0)
    sup = svc.create_supplier("Cloud")
    svc_ = svc.create_service(it.id, "Portal", supplier_ids=[sup.id], outage_cost_per_hour=1_000)
    svc.create_customer("Bank", service_ids=[svc_.id], penalty_per_hour=500)
    return dict(it=it, a=a, b=b, d=d, sup=sup)


def test_drivers_and_risk_factors(svc):
    x = build(svc)
    sc = svc.create_scenario("Crisis", driver_levels={x["d"].id: 2.0}, risk_factors={x["b"].id: {"frequency": 3}})
    r = svc.evaluate_scenario(sc.id)
    by = {k["name"]: k for k in r["risks"]}
    assert by["A"]["after"] == pytest.approx(200_000) and by["B"]["after"] == pytest.approx(300_000)
    assert r["after"]["mean"] == pytest.approx(500_000, rel=0.01) and r["before"]["mean"] == pytest.approx(200_000, rel=0.01)
    assert r["drivers"][0]["to"] == 2.0 and r["missing"] == []
    assert svc.get_driver(x["d"].id).level == 1.0  # evaluating changes nothing


def test_outage_is_costed_like_the_stress_test(svc):
    x = build(svc)
    sc = svc.create_scenario("Cloud down", outage_supplier_id=x["sup"].id, outage_hours=10)
    r = svc.evaluate_scenario(sc.id)
    assert r["outage"]["cost"] == pytest.approx(10 * 1_000 + 10 * 500)
    assert r["after"]["mean"] == pytest.approx(r["before"]["mean"] + 15_000)


def test_overview_sorted_by_tail_and_missing_references(svc):
    x = build(svc)
    svc.create_scenario("Mild", risk_factors={x["a"].id: {"frequency": 1.2}})
    svc.create_scenario("Severe", risk_factors={x["a"].id: {"frequency": 5}, x["b"].id: {"impact": 3}})
    o = svc.scenario_overview()
    assert [s["name"] for s in o["scenarios"]] == ["Severe", "Mild"] and o["today"]["mean"] > 0
    svc.delete_risk(x["b"].id)
    sev = next(s for s in svc.list_scenarios() if s.name == "Severe")
    assert svc.evaluate_scenario(sev.id)["missing"] == [f"risk {x['b'].id}"]


def test_validation(svc):
    x = build(svc)
    for bad in ({"driver_levels": {x["d"].id: 0}}, {"risk_factors": {x["a"].id: {"frequency": -1}}},
                {"outage_supplier_id": x["sup"].id}, {"risk_factors": {x["a"].id: 2}}):
        with pytest.raises(ValidationError):
            svc.create_scenario("Bad", **bad)


def test_excel_round_trip(svc):
    x = build(svc)
    svc.create_scenario("Crisis", driver_levels={x["d"].id: 1.5}, risk_factors={x["b"].id: {"frequency": 2, "impact": 1.5}},
                        outage_supplier_id=x["sup"].id, outage_hours=24)
    assert svc.import_excel(svc.export_excel(), dry_run=True)["changes"]["scenario"]["unchanged"] == 1
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel())
    sc = empty.list_scenarios()[0]
    assert sc.driver_levels == {x["d"].id: 1.5} and sc.risk_factors == {x["b"].id: {"frequency": 2, "impact": 1.5}}


def test_api():
    svc = RiskService(MemoryRepository())
    x = build(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.post("/api/scenarios", json={"name": "Crisis", "driver_levels": {x["d"].id: 2}})
    assert r.status_code == 201, r.text
    assert client.get(f"/api/scenarios/{r.json()['id']}").json()["after"]["mean"] > 0
    t = client.post("/api/scenarios/try", json={"risk_factors": {x["a"].id: {"frequency": 2}}}).json()
    assert t["after"]["mean"] > t["before"]["mean"] and not svc.list_scenarios()[1:]
    assert client.get("/api/scenarios/overview").json()["scenarios"][0]["name"] == "Crisis"
