"""Common shock: one outage costs several customers at once; both engines agree."""

from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository


def build():
    svc = RiskService(MemoryRepository())
    it = svc.create_business_unit("IT")
    cloud = svc.create_supplier("Cloud", tier="CRITICAL")
    portal = svc.create_service(it.id, "Portal", supplier_ids=[cloud.id], outage_cost_per_hour=1_000)
    for name, rate in (("A", 10_000), ("B", 5_000)):
        svc.create_customer(name, service_ids=[portal.id], penalty_per_hour=rate, penalty_grace_hours=2,
                            penalty_cap=200_000)
    r = svc.create_risk(it.id, "Cloud outage", {"type": "poisson", "rate": 1}, {"dist": "fixed", "value": 10_000},
                        supplier_ids=[cloud.id], outage_hours={"dist": "fixed", "value": 12})
    return svc, cloud, r


def test_stress_test_and_scores():
    svc, cloud, r = build()
    st = svc.stress_test(12, supplier_id=cloud.id)
    assert st["own_cost"] == 12_000 and st["penalties"] == 100_000 + 50_000
    # one event = impact + own cost + both penalties (same duration for all)
    assert svc.risk_scores(r)["inherent"]["expected_annual_loss"] == 10_000 + 162_000


def test_engines_agree_and_report_customers():
    svc, _, _ = build()
    a = svc.simulation_report(engine="analytic")
    m = svc.simulation_report(engine="monte_carlo", n_trials=50_000, seed=3)
    assert abs(a["total"]["mean"] - 172_000) / 172_000 < 0.001
    assert abs(m["total"]["mean"] - a["total"]["mean"]) / a["total"]["mean"] < 0.03
    assert [c["name"] for c in a["consequences"]["customers"]] == ["A", "B"]


def test_fingerprint_unchanged_without_outage():
    svc = RiskService(MemoryRepository())
    it = svc.create_business_unit("IT")
    svc.create_risk(it.id, "X", {"type": "poisson", "rate": 1}, {"dist": "fixed", "value": 1})
    assert "outage_hours" not in svc._analysis_inputs(None)["risks"][0]


def test_api_stress_and_fields():
    from fastapi.testclient import TestClient
    from riskmesh.config import Settings
    from riskmesh.presentation.api import create_app
    svc, cloud, r = build()
    with TestClient(create_app(svc, Settings(backend="memory", api_key=None))) as c:
        st = c.get(f"/api/third-parties/stress-test?hours=12&supplier_id={cloud.id}").json()
        assert st["total"] == 162_000 and len(st["costs"]) == 3
        assert c.get("/api/third-parties/stress-test?hours=12").status_code == 422
        terms = c.get(f"/api/risks/{r.id}/consequences").json()
        assert {t["name"] for t in terms} == {"Portal", "A", "B"}
        risk = c.get(f"/api/risks/{r.id}").json()
        assert risk["outage_hours"] == {"dist": "fixed", "value": 12}
        assert risk["scores"]["inherent"]["expected_annual_loss"] == 172_000
        customer = c.get("/api/customers").json()[0]
        assert customer["penalty_per_hour"] == 10_000
        patched = c.patch(f"/api/customers/{customer['id']}", json={"penalty_cap": 50_000}).json()
        assert patched["penalty_cap"] == 50_000
        bad = c.patch(f"/api/risks/{r.id}", json={"outage_hours": {"dist": "uniform", "min": -5, "max": 2}})
        assert bad.status_code == 422
        report = c.post("/api/simulations/compare", params={"engine": "analytic"}).json()
        assert report["residual"]["consequences"]["customers"][0]["name"] == "A"
