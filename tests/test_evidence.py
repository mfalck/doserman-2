"""Observed events, public benchmarks and calibration of estimates (all backends for the service parts)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain import calibration as cal
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository

TODAY = date(2026, 12, 31)
LOGNORM = {"dist": "lognorm_p10_p90", "p10": 10_000, "p90": 1_000_000}


def build(svc):
    group = svc.create_business_unit("Group")
    it = svc.create_business_unit("IT", parent_id=group.id)
    dev = svc.create_business_unit("Dev", parent_id=it.id)
    sales = svc.create_business_unit("Sales", parent_id=group.id)
    dc = svc.create_supplier("DC")
    cloud = svc.create_supplier("Cloud", subcontractor_ids=[dc.id])
    breach = svc.create_risk(it.id, "Breach", {"type": "poisson", "rate": 0.1}, LOGNORM,
                             evidence_categories=["data breach"])
    outage = svc.create_risk(it.id, "Outage", {"type": "poisson", "rate": 1.0}, LOGNORM, supplier_ids=[cloud.id],
                             evidence_categories=["hosting outage"])
    fraud = svc.create_risk(group.id, "Fraud", {"type": "bernoulli", "p": 0.2}, LOGNORM, category="fraud")
    return dict(group=group, it=it, dev=dev, sales=sales, dc=dc, cloud=cloud, breach=breach, outage=outage, fraud=fraud)


def test_levels_decide_which_risks_an_observation_counts_for(svc):
    x = build(svc)
    o_dev = svc.create_observation("2025-01-01", "data breach", business_unit_id=x["dev"].id, source="log")
    svc.create_observation("2025-06-01", "data breach", business_unit_id=x["sales"].id, source="log")  # other branch
    svc.create_observation("2025-07-01", "data breach", source="log")          # group level: not an IT risk
    o_dc = svc.create_observation("2025-08-01", "hosting outage", supplier_id=x["dc"].id, count=3, source="log")
    o_named = svc.create_observation("2025-09-01", "something else", risk_id=x["breach"].id, source="log")
    o_fraud = svc.create_observation("2025-10-01", "Fraud", source="claims")   # group level, risk's own category
    ids = lambda c: {o["id"] for o in c["observations"]}
    assert ids(svc.calibrate_risk(x["breach"].id, today=TODAY)) == {o_dev.id, o_named.id}
    assert ids(svc.calibrate_risk(x["outage"].id, today=TODAY)) == {o_dc.id}  # fourth party counts
    assert ids(svc.calibrate_risk(x["fraud"].id, today=TODAY)) == {o_fraud.id}


def test_period_starts_with_the_source(svc):
    x = build(svc)
    svc.create_observation("2025-01-01", "phishing", business_unit_id=x["it"].id, source="log")
    svc.create_observation("2026-06-01", "data breach", business_unit_id=x["it"].id, source="log")
    c = svc.calibrate_risk(x["breach"].id, today=TODAY)
    assert c["since"] == "2025-01-01" and c["years"] == pytest.approx(2.0, abs=0.01)
    assert svc.calibrate_risk(x["breach"].id, since="2026-01-01", today=TODAY)["years"] == pytest.approx(1.0, abs=0.01)


def test_frequency_moves_with_data_and_benchmarks():
    f = cal.calibrate_frequency({"type": "poisson", "rate": 1.0}, events=0, years=0, benchmarks=[])
    assert f["calibrated_rate"] == pytest.approx(1.0)
    little = cal.calibrate_frequency({"type": "poisson", "rate": 1.0}, 3, 1, [])["calibrated_rate"]
    lots = cal.calibrate_frequency({"type": "poisson", "rate": 1.0}, 30, 10, [])["calibrated_rate"]
    assert 1.0 < little < lots < 3.0 and little == pytest.approx(5 / 3)
    with_bm = cal.calibrate_frequency({"type": "poisson", "rate": 1.0}, 0, 0, [(0.2, 2.0)])
    assert with_bm["calibrated_rate"] == pytest.approx((2 + 0.4) / 4)
    lo, hi = with_bm["interval"]
    assert lo < with_bm["calibrated_rate"] < hi
    b = cal.calibrate_frequency({"type": "bernoulli", "p": 0.2}, 2, 2, [])
    assert b["suggested"]["type"] == "bernoulli" and 0.2 < b["suggested"]["p"] < 1


def test_impact_credibility_weighting():
    same = cal.calibrate_impact(LOGNORM, [], [])
    assert same["suggested"]["p10"] == pytest.approx(10_000, rel=1e-6)
    up = cal.calibrate_impact(LOGNORM, [1_000_000] * 5, [])
    assert up["change_median"] > 0 and up["suggested"]["p90"] > 1_000_000
    assert cal.calibrate_impact({"dist": "pert", "min": 1, "mode": 2, "max": 3}, [5], []) is None


def test_calibrate_uses_residual_fields_and_apply_records_basis(svc):
    x = build(svc)
    svc.update_risk(x["outage"].id, residual_frequency={"type": "poisson", "rate": 0.5})
    for d in ("2025-01-10", "2025-05-10", "2025-09-10", "2026-01-10", "2026-05-10", "2026-09-10"):
        svc.create_observation(d, "hosting outage", supplier_id=x["cloud"].id, loss=200_000, source="log")
    c = svc.calibrate_risk(x["outage"].id)
    assert c["frequency_field"] == "residual_frequency" and c["impact_field"] == "impact"
    assert c["frequency"]["calibrated_rate"] > 0.5
    with svc.context(reason="quarterly review"):
        risk = svc.apply_calibration(x["outage"].id)
    assert risk.residual_frequency["rate"] == pytest.approx(c["frequency"]["calibrated_rate"], rel=1e-4)
    assert risk.impact == c["impact"]["suggested"] and risk.frequency == {"type": "poisson", "rate": 1.0}
    entry = svc.list_audit(entity_type="risk")[0]
    assert "quarterly review" in entry.reason and "6 observed events" in entry.reason


def test_overview_only_lists_risks_with_evidence(svc):
    build(svc)
    assert svc.calibration_overview() == []
    svc.create_benchmark("Public stats", "data breach", events_per_year=0.3, weight=2)
    rows = svc.calibration_overview()
    assert [r["name"] for r in rows] == ["Breach"] and rows[0]["calibrated_rate"] > 0.1


def test_validation(svc):
    x = build(svc)
    with pytest.raises(ValidationError):
        svc.create_observation("2025-13-01", "x")
    with pytest.raises(ValidationError):
        svc.create_observation("2025-01-01", " ")
    with pytest.raises(ValidationError):
        svc.create_observation("2025-01-01", "x", business_unit_id=x["it"].id, supplier_id=x["dc"].id)
    with pytest.raises(ValidationError):
        svc.create_observation("2025-01-01", "x", count=1.5)
    for bad in ({}, {"loss_p10": 10}, {"loss_p10": 10, "loss_p90": 5}, {"events_per_year": 1, "weight": 0}):
        with pytest.raises(ValidationError):
            svc.create_benchmark("B", "x", **bad)


def test_observations_go_with_their_unit_or_supplier(svc):
    x = build(svc)
    svc.create_observation("2025-01-01", "x", business_unit_id=x["dev"].id)
    svc.create_observation("2025-01-01", "x", supplier_id=x["dc"].id)
    keep = svc.create_observation("2025-01-01", "x")
    svc.delete_business_unit(x["dev"].id)
    svc.delete_supplier(x["dc"].id)
    assert [o.id for o in svc.list_observations()] == [keep.id]  # none became group-level


def test_excel_round_trip_and_hand_made_rows(svc):
    x = build(svc)
    svc.create_observation("2025-01-01", "data breach", business_unit_id=x["it"].id, loss=5_000, count=2)
    svc.create_benchmark("Stats", "data breach", events_per_year=0.2, loss_p10=1_000, loss_p90=90_000, weight=3)
    content = svc.export_excel()
    preview = svc.import_excel(content, dry_run=True)
    assert preview["changes"]["observation"]["unchanged"] == 1 and preview["changes"]["benchmark"]["unchanged"] == 1
    import io

    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content))
    ws = wb["Observations"]
    headers = [c.value.rstrip(" *") for c in ws[1]]
    row = [None] * len(headers)
    row[headers.index("Date")], row[headers.index("Category")] = "2026-02-01", "data breach"
    row[headers.index("Organisation")] = "Dev"
    ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    result = svc.import_excel(buf.getvalue())
    assert result["changes"]["observation"]["created"] == 1
    new = next(o for o in svc.list_observations() if o.date == "2026-02-01")
    assert new.count == 1 and new.business_unit_id == x["dev"].id


def test_api():
    svc = RiskService(MemoryRepository())
    x = build(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.post("/api/observations", json={"date": "2025-03-01", "category": "data breach",
                                               "business_unit_id": x["it"].id, "loss": 50_000, "source": "log"})
    assert r.status_code == 201, r.text
    r = client.post("/api/benchmarks", json={"name": "Stats", "category": "data breach", "events_per_year": 0.3})
    assert r.status_code == 201, r.text
    assert client.get("/api/evidence/categories").json() == ["data breach", "hosting outage"]
    rows = client.get("/api/calibration").json()
    assert rows[0]["name"] == "Breach"
    c = client.get(f"/api/risks/{x['breach'].id}/calibration").json()
    assert c["frequency"]["events"] == 1 and len(c["benchmarks"]) == 1
    r = client.post(f"/api/risks/{x['breach'].id}/calibration/apply", json={"impact": False})
    assert r.status_code == 200 and r.json()["frequency"]["rate"] != 0.1 and r.json()["impact"] == LOGNORM
