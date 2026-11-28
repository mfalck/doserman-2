"""Assessment methods as plugins: ISO 27005 and FAIR on top of the common model."""

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain import methods
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository

FAIR_INPUTS = {"tef": {"min": 2, "mode": 6, "max": 20}, "vulnerability": {"min": 0.01, "mode": 0.03, "max": 0.1},
               "primary_loss": {"min": 100_000, "mode": 500_000, "max": 3_000_000},
               "secondary_probability": 0.3, "secondary_loss": {"min": 50_000, "mode": 300_000, "max": 2_000_000},
               "residual_vulnerability": 0.01}


@pytest.mark.parametrize("lk,c", [(lk, c) for lk in range(1, 6) for c in range(1, 6)])
def test_iso_levels_land_in_the_band_they_were_rated(lk, c):
    svc = RiskService(MemoryRepository())
    it = svc.create_business_unit("IT")
    r = svc.create_risk(it.id, "R", method="iso27005", method_inputs={"likelihood": lk, "consequence": c})
    sc = svc.risk_scores(r)["inherent"]
    assert (sc["likelihood"], sc["impact"]) == (lk, c)


def test_fair_keeps_the_expected_values(svc):
    it = svc.create_business_unit("IT")
    r = svc.create_risk(it.id, "Ransomware", method="fair", method_inputs=FAIR_INPUTS)
    lef = (2 + 4 * 6 + 20) / 6 * (0.01 + 4 * 0.03 + 0.1) / 6
    assert r.frequency["rate"] == pytest.approx(lef, rel=1e-4)
    mean_loss = (100_000 + 4 * 500_000 + 3_000_000) / 6 + 0.3 * (50_000 + 4 * 300_000 + 2_000_000) / 6
    eal = svc.risk_scores(r)["inherent"]["expected_annual_loss"]
    assert eal == pytest.approx(lef * mean_loss, rel=0.01)
    assert r.residual_frequency["rate"] == pytest.approx(lef / 0.03 * 0.01 * 0.03 / ((0.01 + 0.12 + 0.1) / 6), rel=1e-3)
    assert r.method_inputs["method_version"] == "1"
    assert "LEF" in methods.get("fair").summary(r.method_inputs)


def test_validation_and_switching_back(svc):
    it = svc.create_business_unit("IT")
    for inputs in ({"likelihood": 6, "consequence": 2}, {"likelihood": 2}, {"likelihood": 2.5, "consequence": 2},
                   {"likelihood": 2, "consequence": 2, "residual_likelihood": 1}):
        with pytest.raises(ValidationError):
            svc.create_risk(it.id, "Bad", method="iso27005", method_inputs=inputs)
    with pytest.raises(ValidationError):
        svc.create_risk(it.id, "Bad", method="fair", method_inputs={**FAIR_INPUTS, "vulnerability": 1.5})
    with pytest.raises(ValidationError):
        svc.create_risk(it.id, "Bad", method="nope", method_inputs={})
    with pytest.raises(ValidationError):
        svc.create_risk(it.id, "Bad")  # neither specs nor a method
    r = svc.create_risk(it.id, "R", method="iso27005", method_inputs={"likelihood": 2, "consequence": 2})
    r = svc.update_risk(r.id, method_inputs={"likelihood": 5, "consequence": 2})
    assert svc.risk_scores(r)["inherent"]["likelihood"] == 5
    r = svc.update_risk(r.id, method="quantitative", frequency={"type": "poisson", "rate": 1}, impact={"dist": "fixed", "value": 10})
    assert r.method_inputs == {} and r.frequency["rate"] == 1


def test_plugins_can_be_registered():
    class Flat(methods.RiskMethod):
        key, label = "flat", "Flat"

        def validate(self, inputs):
            return {"loss": float(inputs["loss"])}

        def to_specs(self, inputs, scale):
            return {"frequency": {"type": "bernoulli", "p": 1.0}, "impact": {"dist": "fixed", "value": inputs["loss"]},
                    "residual_frequency": None, "residual_impact": None}
    methods.register(Flat())
    svc = RiskService(MemoryRepository())
    it = svc.create_business_unit("IT")
    r = svc.create_risk(it.id, "R", method="flat", method_inputs={"loss": 123})
    assert svc.risk_scores(r)["inherent"]["expected_annual_loss"] == 123


def test_excel_round_trip_keeps_method(svc):
    it = svc.create_business_unit("IT")
    svc.create_risk(it.id, "Ransomware", method="fair", method_inputs=FAIR_INPUTS)
    svc.create_risk(it.id, "People", method="iso27005", method_inputs={"likelihood": 3, "consequence": 4})
    assert svc.import_excel(svc.export_excel(), dry_run=True)["changes"]["risk"]["unchanged"] == 2
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel())
    assert sorted(r.method for r in empty.list_risks()) == ["fair", "iso27005"]


def test_api():
    svc = RiskService(MemoryRepository())
    it = svc.create_business_unit("IT")
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    keys = [m["key"] for m in client.get("/api/methods").json()]
    assert keys[0] == "quantitative" and {"iso27005", "fair"} <= set(keys)
    r = client.post("/api/risks", json={"name": "R", "business_unit_id": it.id, "method": "iso27005",
                                        "method_inputs": {"likelihood": 4, "consequence": 3}})
    assert r.status_code == 201, r.text
    assert r.json()["method_summary"].startswith("L4 x C3 = 12")
    assert client.post("/api/risks", json={"name": "X", "business_unit_id": it.id}).status_code in (400, 422)
