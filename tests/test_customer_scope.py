"""Analyses and extracts for exactly one customer."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from riskmesh.config import Settings
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.services.demo import seed_demo
from riskmesh.storage import MemoryRepository


def bank(svc):
    return next(c for c in svc.list_customers() if c.name == "East Savings Bank")


def test_scope_has_only_the_risks_to_the_customers_services(svc):
    seed_demo(svc)
    r = svc.simulation_report(unit_id=f"customer:{bank(svc).id}")
    names = {x["name"] for x in r["risks"]}
    assert names == {"Database outage", "Ransomware attack", "Web app data breach"}
    assert r["scope"]["perspective"] == "ours" and r["scope"]["customer"] == "East Savings Bank"
    assert r["total"]["mean"] < svc.simulation_report()["total"]["mean"]
    assert "appetite" not in r  # the organisation's appetite is not the customer's


def test_customers_own_loss(svc):
    seed_demo(svc)
    c = bank(svc)
    with pytest.raises(ValidationError):
        svc.simulation_report(unit_id=f"customer-own:{c.id}")
    svc.update_customer(c.id, own_loss_per_outage_hour=50_000)
    r = svc.simulation_report(unit_id=f"customer-own:{c.id}")
    assert [x["name"] for x in r["risks"]] == ["Database outage"]  # the only one that takes the portal down
    assert set(r["scope"]["left_out"]) == {"Ransomware attack", "Web app data breach"}
    assert r["total"]["mean"] > 0
    mc = svc.simulation_report(50_000, 5, unit_id=f"customer-own:{c.id}", engine="monte_carlo")
    assert mc["total"]["mean"] == pytest.approx(r["total"]["mean"], rel=0.05)


def test_customer_runs_are_stored_and_reproducible(svc):
    seed_demo(svc)
    run = svc.run_analysis(unit_id=f"customer:{bank(svc).id}")
    assert svc.run_analysis(unit_id=f"customer:{bank(svc).id}")["run"]["cached"]
    assert svc.reproduce_run(run["run"]["id"])["reproducible"]


def test_report_includes_both_perspectives(svc):
    seed_demo(svc)
    c = bank(svc)
    svc.update_customer(c.id, own_loss_per_outage_hour=50_000)
    r = svc.customer_report(c.id)
    assert r["analysis"]["ours"]["p80"] > 0 and r["analysis"]["customer"]["mean"] > 0
    assert "Your loss from outages of our services" in svc.customer_report_html(c.id)


def test_workbook_with_only_the_customers_part(svc):
    seed_demo(svc)
    c = bank(svc)
    wb = load_workbook(io.BytesIO(svc.export_excel(c.id)))

    def names(sheet):
        ws = wb[sheet]
        col = [cell.value for cell in ws[1]].index("Name *") + 1
        return {ws.cell(row=r, column=col).value for r in range(2, ws.max_row + 1)}
    assert names("Customers") == {"East Savings Bank"}
    assert names("Risks") == {"Database outage", "Ransomware attack", "Web app data breach"}
    assert "Payroll processing" not in names("Services") and wb["Appetite"].max_row == 1
    # the part can start a separate analysis: it imports into an empty store without errors
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel(c.id))
    assert len(empty.list_risks()) == 3 and len(empty.list_customers()) == 1


def test_api():
    svc = RiskService(MemoryRepository())
    seed_demo(svc)
    cid = bank(svc).id
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.post("/api/simulations", json={"unit_id": f"customer:{cid}"})
    assert r.status_code == 200 and r.json()["scope"]["customer_id"] == cid
    x = client.get(f"/api/customers/{cid}/export.xlsx")
    assert x.status_code == 200 and "East-Savings-Bank" in x.headers["content-disposition"]
