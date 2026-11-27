"""Suppliers, services, customers: the dependency chain and what it tells us (all backends)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain.errors import NotFoundError, ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.storage import MemoryRepository


def build(svc):
    it = svc.create_business_unit("IT")
    dc = svc.create_supplier("DataCentre", tier="IMPORTANT", country="DE")
    cloud = svc.create_supplier("Cloud", tier="CRITICAL", subcontractor_ids=[dc.id], next_review="2020-01-01")
    mail = svc.create_supplier("Mail", exit_plan=True)
    portal = svc.create_service(it.id, "Portal", criticality="CRITICAL", supplier_ids=[cloud.id, mail.id],
                                rto_hours=4)
    hub = svc.create_service(it.id, "Hub", criticality="HIGH", supplier_ids=[cloud.id])
    bank = svc.create_customer("Bank", sector="finance", regulations=["DORA"], service_ids=[portal.id],
                               contract_value=2_000_000, critical_function=True, notification_hours=4,
                               audit_rights=True)
    town = svc.create_customer("Town", regulations=["NIS2_IMPORTANT"], service_ids=[portal.id, hub.id],
                               contract_value=1_000_000, notification_hours=24)
    outage = svc.create_risk(it.id, "Cloud outage", {"type": "poisson", "rate": 1}, {"dist": "fixed", "value": 10},
                             service_ids=[portal.id, hub.id], supplier_ids=[cloud.id])
    return dict(it=it, dc=dc, cloud=cloud, mail=mail, portal=portal, hub=hub, bank=bank, town=town, outage=outage)


def test_fourth_party_failure_reaches_customers(svc):
    x = build(svc)
    e = svc.supplier_exposure(x["dc"].id)
    assert [v["name"] for v in e["via_suppliers"]] == ["Cloud"]
    assert {s["name"]: s["depth"] for s in e["services"]} == {"Hub": 2, "Portal": 2}
    assert e["customer_count"] == 2 and e["contract_value"] == 3_000_000
    assert e["critical_function_customers"] == 1 and e["shortest_notification_hours"] == 4
    assert e["by_regulation"] == {"DORA": 1, "NIS2_IMPORTANT": 1}
    assert svc.supplier_exposure(x["mail"].id)["customer_count"] == 2
    assert [r["name"] for r in svc.supplier_exposure(x["cloud"].id)["risks"]] == ["Cloud outage"]


def test_service_exposure_and_customer_profile(svc):
    x = build(svc)
    e = svc.service_exposure(x["hub"].id)
    assert [(s["name"], s["depth"]) for s in e["suppliers"]] == [("Cloud", 1), ("DataCentre", 2)]
    assert [c["name"] for c in e["customers"]] == ["Town"]
    p = svc.customer_profile(x["bank"].id)
    assert [s["name"] for s in p["services"]] == ["Portal"]
    assert [(s["name"], s["depth"]) for s in p["suppliers"]] == [("Cloud", 1), ("Mail", 1), ("DataCentre", 2)]
    assert [r["name"] for r in p["risks"]] == ["Cloud outage"]
    assert any("exit support" in f["finding"] for f in p["findings"])


def test_overview_ranks_concentration_and_findings(svc):
    x = build(svc)
    o = svc.third_party_overview()
    assert o["totals"]["customers"] == 2 and o["totals"]["contract_value"] == 3_000_000
    top = o["suppliers"][0]
    assert top["name"] in ("Cloud", "DataCentre") and top["customer_count"] == 2 and top["service_count"] == 2
    found = {(f["name"], f["finding"]) for f in o["findings"]}
    assert ("Cloud", "Review overdue since 2020-01-01") in found
    assert ("Cloud", "Critical supplier without an exit plan") in found
    assert ("Bank", "DORA customer, contract lacks: exit support") in found
    assert ("Hub", "Critical service without a recovery time objective (RTO)") in found
    soon = svc.third_party_findings(today=date(2019, 12, 1))
    assert not any(f["finding"].startswith("Review overdue") for f in soon)
    svc.update_supplier(x["cloud"].id, exit_plan=True, next_review="2099-01-01")
    assert not any(f["name"] == "Cloud" and f["severity"] == "high" for f in svc.third_party_findings())


def test_validation_and_references(svc):
    x = build(svc)
    with pytest.raises(ValidationError):
        svc.create_supplier("Bad", tier="HUGE")
    with pytest.raises(ValidationError):
        svc.create_customer("Bad", regulations=["NIS3"])
    with pytest.raises(ValidationError):
        svc.update_supplier(x["cloud"].id, subcontractor_ids=[x["cloud"].id])
    with pytest.raises(ValidationError):
        svc.create_service(x["it"].id, "Bad", rto_hours=-1)
    with pytest.raises(NotFoundError):
        svc.create_service(x["it"].id, "Bad", supplier_ids=["nope"])
    # a cycle in the subcontractor chain is harmless
    svc.update_supplier(x["dc"].id, subcontractor_ids=[x["cloud"].id])
    assert svc.supplier_exposure(x["dc"].id)["customer_count"] == 2


def test_delete_unlinks_and_audits(svc):
    x = build(svc)
    svc.delete_supplier(x["cloud"].id)
    assert svc.get_service(x["portal"].id).supplier_ids == [x["mail"].id]
    assert svc.get_risk(x["outage"].id).supplier_ids == []
    svc.delete_service(x["portal"].id)
    assert svc.get_customer(x["bank"].id).service_ids == []
    assert svc.get_risk(x["outage"].id).service_ids == [x["hub"].id]
    svc.delete_business_unit(x["it"].id)  # services belong to a unit
    assert svc.list_services() == [] and svc.get_customer(x["town"].id).service_ids == []
    assert svc.verify_audit()["ok"]


def test_export_import_round_trip(svc):
    build(svc)
    data = svc.export_data()
    assert len(data["suppliers"]) == 3 and len(data["services"]) == 2 and len(data["customers"]) == 2
    other = RiskService(MemoryRepository())
    other.import_data(data)
    assert other.third_party_overview()["totals"] == svc.third_party_overview()["totals"]


def test_api():
    svc = RiskService(MemoryRepository())
    with TestClient(create_app(svc, Settings(backend="memory", api_key=None))) as c:
        bu = c.post("/api/business-units", json={"name": "IT"}).json()
        sup = c.post("/api/suppliers", json={"name": "Cloud", "tier": "CRITICAL", "data_locations": ["DK"]})
        assert sup.status_code == 201, sup.text
        ser = c.post("/api/services", json={"name": "Portal", "business_unit_id": bu["id"],
                                            "supplier_ids": [sup.json()["id"]]}).json()
        cus = c.post("/api/customers", json={"name": "Bank", "regulations": ["DORA"], "service_ids": [ser["id"]],
                                             "contract_value": 5}).json()
        assert c.post("/api/customers", json={"name": "X", "regulations": ["NIS3"]}).status_code == 422
        assert c.get(f"/api/suppliers/{sup.json()['id']}/exposure").json()["contract_value"] == 5
        assert c.get(f"/api/services/{ser['id']}/exposure").json()["customer_count"] == 1
        assert c.get(f"/api/customers/{cus['id']}/profile").json()["suppliers"][0]["name"] == "Cloud"
        assert c.get("/api/third-parties/overview").json()["totals"]["suppliers"] == 1
        assert c.patch(f"/api/customers/{cus['id']}", json={"exit_support": True}).json()["exit_support"]
        assert c.get("/api/customers?regulation=DORA").json()[0]["name"] == "Bank"
        r = c.post("/api/risks", json={"name": "Outage", "business_unit_id": bu["id"], "service_ids": [ser["id"]],
                                       "frequency": {"type": "poisson", "rate": 1},
                                       "impact": {"dist": "fixed", "value": 1}}).json()
        assert r["service_ids"] == [ser["id"]]
        assert "DORA" in c.get("/api/meta").json()["regulations"]
        assert c.delete(f"/api/suppliers/{sup.json()['id']}").status_code == 204
