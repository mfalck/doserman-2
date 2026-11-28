"""Objectives: mapping risks to them and the risk to each objective (all backends for the service parts)."""

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository

FIXED = {"dist": "fixed", "value": 100_000}


def build(svc):
    hq = svc.create_business_unit("HQ")
    it = svc.create_business_unit("IT", parent_id=hq.id)
    dc = svc.create_supplier("DC")
    cloud = svc.create_supplier("Cloud", subcontractor_ids=[dc.id])
    portal = svc.create_service(it.id, "Portal", supplier_ids=[cloud.id])
    payroll = svc.create_service(hq.id, "Payroll")
    avail = svc.create_objective("Portal available", criticality="MISSION_CRITICAL", service_ids=[portal.id],
                                 tolerance_loss=150_000, tolerance_probability=0.10)
    growth = svc.create_objective("Growth", business_unit_id=it.id)
    empty = svc.create_objective("Payroll on time", service_ids=[payroll.id], criticality="MISSION_CRITICAL")
    outage = svc.create_risk(it.id, "Outage", {"type": "bernoulli", "p": 0.5}, FIXED, service_ids=[portal.id])
    fourth = svc.create_risk(it.id, "DC fire", {"type": "bernoulli", "p": 0.1}, FIXED, supplier_ids=[dc.id])
    churn = svc.create_risk(it.id, "Churn", {"type": "bernoulli", "p": 0.2}, FIXED, objective_ids=[growth.id])
    other = svc.create_risk(hq.id, "Other", {"type": "bernoulli", "p": 0.2}, FIXED)
    return dict(hq=hq, it=it, dc=dc, portal=portal, avail=avail, growth=growth, empty=empty, outage=outage,
                fourth=fourth, churn=churn, other=other)


def test_threats_direct_via_service_and_via_fourth_party(svc):
    build(svc)
    o = svc.objective_overview()
    by = {r["name"]: r for r in o["objectives"]}
    assert {t["name"]: t["via"] for t in by["Portal available"]["threats"]} == {
        "Outage": "service Portal", "DC fire": "supplier DC"}
    assert [t["via"] for t in by["Growth"]["threats"]] == ["direct"]
    assert by["Payroll on time"]["threats"] == []
    assert [r["name"] for r in o["unmapped_risks"]] == ["Other"]


def test_risk_to_objective_and_tolerance_in_both_engines(svc):
    x = build(svc)
    for engine in ("analytic", "monte_carlo"):
        rep = svc.simulation_report(20_000, 1, engine=engine)
        obj = {r["name"]: r for r in rep["objectives"]}
        avail = obj["Portal available"]
        assert avail["risk_count"] == 2
        assert avail["mean"] == pytest.approx(0.6 * 100_000, rel=0.03)
        # P(loss > 150k) = both happen = 0.05 < 0.10
        assert avail["tolerance"]["actual"] == pytest.approx(0.05, abs=0.01) and not avail["tolerance"]["breached"]
        assert obj["Growth"]["tolerance"] is None
    svc.update_objective(x["avail"].id, tolerance_loss=50_000)  # P(loss > 50k) = 1 - 0.5*0.9 = 0.55
    fig = next(r for r in svc.simulation_report()["objectives"] if r["name"] == "Portal available")
    assert fig["tolerance"]["breached"] and fig["tolerance"]["actual"] == pytest.approx(0.55, abs=0.01)


def test_findings(svc):
    build(svc)
    findings = {(f["objective"], f["finding"].split(":")[0]) for f in svc.objective_overview()["findings"]}
    assert ("Payroll on time", "No risks mapped") in findings
    assert ("Payroll on time", "No owner") in findings
    assert ("Payroll on time", "No tolerance") in findings


def test_validation_and_deletes(svc):
    x = build(svc)
    with pytest.raises(ValidationError):
        svc.create_objective("Bad", criticality="URGENT")
    with pytest.raises(ValidationError):
        svc.create_objective("Half", tolerance_loss=1000)
    with pytest.raises(ValidationError):
        svc.create_objective("Pct", tolerance_loss=1000, tolerance_probability=5)
    svc.delete_objective(x["growth"].id)
    assert svc.get_risk(x["churn"].id).objective_ids == []
    unit_obj = svc.create_objective("IT goal", business_unit_id=x["it"].id)
    svc.delete_business_unit(x["it"].id)
    assert unit_obj.id not in {o.id for o in svc.list_objectives()}  # not turned into an organisation-wide one


def test_objectives_change_the_fingerprint_only_when_set(svc):
    hq = svc.create_business_unit("HQ")
    svc.create_risk(hq.id, "R", {"type": "bernoulli", "p": 0.2}, FIXED)
    params = svc._run_params("compare", "analytic", 0, None, True, None)
    before = svc.analysis_fingerprint(params)
    o = svc.create_objective("Goal")
    assert svc.analysis_fingerprint(params) != before
    svc.delete_objective(o.id)
    assert svc.analysis_fingerprint(params) == before


def test_excel_round_trip(svc):
    build(svc)
    preview = svc.import_excel(svc.export_excel(), dry_run=True)
    assert preview["changes"]["objective"]["unchanged"] == 3 and preview["changes"]["risk"]["unchanged"] == 4
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel())
    a = next(o for o in empty.list_objectives() if o.name == "Portal available")
    assert a.tolerance_probability == pytest.approx(0.10) and a.criticality == "MISSION_CRITICAL"
    assert next(r for r in empty.list_risks() if r.name == "Churn").objective_ids


def test_api():
    svc = RiskService(MemoryRepository())
    x = build(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.post("/api/objectives", json={"name": "New", "criticality": "SUPPORTING", "service_ids": [x["portal"].id]})
    assert r.status_code == 201, r.text
    assert client.post("/api/objectives", json={"name": "X", "tolerance_probability": 2}).status_code == 422
    o = client.get("/api/objectives/overview").json()
    assert {r["name"] for r in o["objectives"]} >= {"New", "Portal available"}
    r = client.patch(f"/api/risks/{x['other'].id}", json={"objective_ids": [x["growth"].id]})
    assert r.status_code == 200 and r.json()["objective_ids"] == [x["growth"].id]
    assert client.delete(f"/api/objectives/{x['growth'].id}").status_code == 204
