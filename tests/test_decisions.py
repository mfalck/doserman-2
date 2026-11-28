"""Decision insight: what controls are worth and what to fund (all backends for the service parts)."""

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain import decisions as dec
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository

FIXED = {"dist": "fixed", "value": 100_000}


def build(svc):
    it = svc.create_business_unit("IT")
    a = svc.create_risk(it.id, "A", {"type": "poisson", "rate": 1.0}, FIXED)          # EAL 100k
    b = svc.create_risk(it.id, "B", {"type": "poisson", "rate": 1.0}, FIXED,
                        residual_frequency={"type": "poisson", "rate": 0.5})          # EAL 50k with controls
    have = svc.create_control(it.id, "Have", implementation_status="IMPLEMENTED", risk_ids=[b.id],
                              annual_cost=10_000, frequency_reduction=0.5)
    cheap = svc.create_control(it.id, "Cheap", risk_ids=[a.id], implementation_cost=30_000, annual_cost=10_000,
                               frequency_reduction=0.5)                              # removes 50k, costs 20k/yr
    dear = svc.create_control(it.id, "Dear", risk_ids=[a.id], implementation_cost=300_000, impact_reduction=0.5)
    vague = svc.create_control(it.id, "Vague", risk_ids=[a.id])
    return dict(it=it, a=a, b=b, have=have, cheap=cheap, dear=dear, vague=vague)


def test_effects_scale_specs():
    assert dec.scale_frequency({"type": "bernoulli", "p": 0.6}, 2) == {"type": "bernoulli", "p": 1.0}
    assert dec.scale_impact({"dist": "pert", "min": 10, "mode": 20, "max": 40}, 0.5)["max"] == 20
    assert dec.scale_impact({"dist": "expon", "params": {}}, 0.5) is None
    r = {"frequency": {"type": "poisson", "rate": 2}, "impact": FIXED, "outage_hours": {"dist": "fixed", "value": 10}}
    after = dec.apply_effect(r, 0.5, 0.2)
    assert after["residual_frequency"]["rate"] == 1 and after["residual_impact"]["value"] == 80_000
    assert after["residual_outage_hours"]["value"] == 8
    back = dec.apply_effect(after, 0.5, 0.2, remove=True)
    assert back["residual_frequency"]["rate"] == pytest.approx(2)
    assert dec.annualised_cost(300, 10, 3) == 110


def test_control_values(svc):
    build(svc)
    v = {c["name"]: c for c in svc.control_values()["controls"]}
    assert v["Cheap"]["benefit"] == pytest.approx(50_000) and v["Cheap"]["annualised_cost"] == pytest.approx(20_000)
    assert v["Cheap"]["net_benefit"] == pytest.approx(30_000) and v["Cheap"]["rosi"] == pytest.approx(1.5)
    assert v["Cheap"]["payback_years"] == pytest.approx(30_000 / 40_000)
    # implemented: what we would lose without it (B back to 100k)
    assert v["Have"]["benefit"] == pytest.approx(50_000) and v["Have"]["p99_without"] > v["Have"]["p99_with"]
    assert v["Dear"]["net_benefit"] == pytest.approx(50_000 - 100_000)
    missing = {m["name"]: m["missing"] for m in svc.control_values()["not_quantified"]}
    assert missing == {"Vague": ["effect", "cost"]}


def test_plan_respects_budget_and_does_not_double_count(svc):
    build(svc)
    p = svc.investment_plan(50_000)
    assert [c["name"] for c in p["chosen"]] == ["Cheap"] and p["spent"] == 40_000
    assert p["after"]["mean"] == pytest.approx(p["before"]["mean"] - 50_000, rel=1e-3)
    both = svc.investment_plan(1_000_000)
    assert [c["name"] for c in both["chosen"]] == ["Cheap", "Dear"]
    # Dear on top of Cheap: halves the loss of the remaining 50k -> 25k, not 50k
    assert both["chosen"][1]["benefit"] == pytest.approx(25_000)
    assert svc.investment_plan(0)["chosen"] == []
    with pytest.raises(ValidationError):
        svc.investment_plan(-1)


def test_plan_shows_objectives_and_appetite(svc):
    x = build(svc)
    svc.create_objective("Keep A low", tolerance_loss=150_000, tolerance_probability=0.2)
    svc.update_risk(x["a"].id, objective_ids=[svc.list_objectives()[0].id])
    svc.create_appetite("Group", [{"loss": 250_000, "probability": 0.2}])
    p = svc.investment_plan(1_000_000)
    assert p["before"]["objectives"][0]["breached"] and not p["after"]["objectives"][0]["breached"]
    assert len(p["after"]["appetite"]) == 1


def test_implement_builds_the_effect_into_the_residual(svc):
    x = build(svc)
    with svc.context(reason="board decision"):
        c = svc.implement_control(x["cheap"].id)
    assert c.implementation_status == "IMPLEMENTED"
    assert svc.get_risk(x["a"].id).residual_frequency == {"type": "poisson", "rate": 0.5}
    # now valued as implemented: the same 50k, not counted again on top
    v = {c["name"]: c for c in svc.control_values()["controls"]}
    assert v["Cheap"]["benefit"] == pytest.approx(50_000)
    assert "board decision" in svc.list_audit(entity_type="risk")[0].reason
    with pytest.raises(ValidationError):
        svc.implement_control(x["cheap"].id)
    with pytest.raises(ValidationError):
        svc.implement_control(x["vague"].id)


def test_validation(svc):
    it = svc.create_business_unit("IT")
    for bad in ({"frequency_reduction": 1.0}, {"impact_reduction": -0.1}, {"annual_cost": -5}, {"lifetime_years": 0}):
        with pytest.raises(ValidationError):
            svc.create_control(it.id, "X", **bad)


def test_excel_round_trip(svc):
    build(svc)
    preview = svc.import_excel(svc.export_excel(), dry_run=True)
    assert preview["changes"]["control"]["unchanged"] == 4
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel())
    c = next(c for c in empty.list_controls() if c.name == "Cheap")
    assert (c.frequency_reduction, c.implementation_cost, c.lifetime_years) == (0.5, 30_000, 3.0)


def test_api():
    svc = RiskService(MemoryRepository())
    x = build(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    v = client.get("/api/decisions/controls").json()
    assert {c["name"] for c in v["controls"]} == {"Have", "Cheap", "Dear"}
    p = client.get("/api/decisions/plan", params={"budget": 50_000}).json()
    assert [c["name"] for c in p["chosen"]] == ["Cheap"]
    assert client.patch(f"/api/controls/{x['vague'].id}", json={"frequency_reduction": 1.5}).status_code == 422
    r = client.post(f"/api/controls/{x['cheap'].id}/implement")
    assert r.status_code == 200 and r.json()["implementation_status"] == "IMPLEMENTED"


def build_with_driver(svc):
    x = build(svc)
    d = svc.create_driver("Hygiene")
    svc.create_driver_effect(d.id, x["a"].id, frequency_sensitivity=1.0)
    svc.create_driver_effect(d.id, x["b"].id, frequency_sensitivity=1.0)
    auto = svc.create_control(x["it"].id, "Automation", implementation_cost=30_000, annual_cost=10_000,
                              driver_id=d.id, driver_reduction=0.5)
    return {**x, "driver": d, "auto": auto}


def test_control_through_a_driver_helps_every_driven_risk(svc):
    build_with_driver(svc)
    v = {c["name"]: c for c in svc.control_values()["controls"]}
    auto = v["Automation"]
    # A 100k -> 50k, B 50k -> 25k: no direct link to either risk
    assert auto["benefit"] == pytest.approx(75_000) and auto["risk_count"] == 2
    assert auto["driver"]["name"] == "Hygiene" and auto["p99_without"] > auto["p99_with"]


def test_plan_and_implement_through_a_driver(svc):
    x = build_with_driver(svc)
    p = svc.investment_plan(1_000_000)
    names = [c["name"] for c in p["chosen"]]
    assert names[0] == "Automation" and p["chosen"][0]["via_driver"] == "Hygiene"
    # Cheap on top of the halved driver: A 50k -> 25k
    assert next(c for c in p["chosen"] if c["name"] == "Cheap")["benefit"] == pytest.approx(25_000)
    svc.implement_control(x["auto"].id)
    assert svc.get_driver(x["driver"].id).level == pytest.approx(0.5)
    v = {c["name"]: c for c in svc.control_values()["controls"]}
    assert v["Automation"]["status"] == "IMPLEMENTED" and v["Automation"]["benefit"] == pytest.approx(75_000)


def test_driver_reduction_validation(svc):
    it = svc.create_business_unit("IT")
    d = svc.create_driver("D")
    with pytest.raises(ValidationError):
        svc.create_control(it.id, "X", driver_reduction=0.3)
    with pytest.raises(ValidationError):
        svc.create_control(it.id, "Y", driver_id=d.id, driver_reduction=0.95)
