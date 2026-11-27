"""Incident register (deadlines) and the DORA register extract (all backends for the service parts)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.services.dora import lei_is_valid
from riskmesh.services.incidents import add_month
from riskmesh.storage import MemoryRepository

LEI_US = "DEMO00RISKMESH000138"
LEI_CLOUD = "DEMO00NORDICCLOUD134"


def build(svc):
    hq = svc.create_business_unit("Us", organization_type="COMPANY", lei=LEI_US, country="DK")
    dc = svc.create_supplier("DC", country="DE", company_number="DE123")
    cloud = svc.create_supplier("Cloud", tier="CRITICAL", country="DK", lei=LEI_CLOUD, subcontractor_ids=[dc.id])
    portal = svc.create_service(hq.id, "Portal", supplier_ids=[cloud.id], ict_service_type="SaaS",
                                data_locations=["DK"])
    hub = svc.create_service(hq.id, "Hub")
    bank = svc.create_customer("Bank", regulations=["DORA"], service_ids=[portal.id], notification_hours=4,
                               critical_function=True)
    town = svc.create_customer("Town", regulations=["NIS2_IMPORTANT"], service_ids=[portal.id, hub.id])
    other = svc.create_customer("Other", service_ids=[hub.id], notification_hours=48)
    return dict(hq=hq, dc=dc, cloud=cloud, portal=portal, hub=hub, bank=bank, town=town, other=other)


NOW = datetime(2026, 11, 27, 12, 0)


def by_key(d):
    return {x["key"]: x for x in d["deadlines"]}


def test_supplier_incident_reaches_customers_through_the_chain(svc):
    x = build(svc)
    inc = svc.create_incident("Power loss", "2026-11-27T09:00", supplier_ids=[x["dc"].id])
    d = svc.incident_deadlines(inc.id, now=NOW)
    assert [s["name"] for s in d["services"]] == ["Portal"]
    assert sorted(c["name"] for c in d["customers"]) == ["Bank", "Town"]
    k = by_key(d)
    assert not any(key.startswith("nis2:") for key in k)  # not assessed: no reporting clock shown
    assert k[f"customer:{x['bank'].id}"]["due"] == "2026-11-27T13:00"
    assert k[f"customer:{x['bank'].id}"]["status"] == "due_soon" and "DORA" in k[f"customer:{x['bank'].id}"]["note"]
    assert k[f"customer:{x['town'].id}"]["status"] == "no_deadline"


def test_nis2_deadlines_and_final_report_after_notification(svc):
    x = build(svc)
    inc = svc.create_incident("Outage", "2026-11-26T08:00", service_ids=[x["hub"].id], nis2_significant=True)
    k = by_key(svc.incident_deadlines(inc.id, now=NOW))
    assert k["nis2:early_warning"]["due"] == "2026-11-27T08:00" and k["nis2:early_warning"]["status"] == "overdue"
    assert k["nis2:notification"]["due"] == "2026-11-29T08:00" and k["nis2:notification"]["status"] == "pending"
    assert k["nis2:final"]["due"] == "2026-12-29T08:00"
    svc.record_notification(inc.id, "nis2:early_warning", "2026-11-27T09:30")
    svc.record_notification(inc.id, "nis2:notification", "2026-11-28T10:00")
    k = by_key(svc.incident_deadlines(inc.id, now=NOW))
    assert k["nis2:early_warning"]["status"] == "late" and k["nis2:notification"]["status"] == "sent"
    assert k["nis2:final"]["due"] == "2026-12-28T10:00"  # one month after the notification was sent
    svc.record_notification(inc.id, "nis2:notification", "")  # undo
    assert "nis2:notification" not in svc.get_incident(inc.id).notifications
    with pytest.raises(ValidationError):
        svc.record_notification(inc.id, "customer:nobody")


def test_closed_incident_has_no_running_clock(svc):
    x = build(svc)
    inc = svc.create_incident("Old", "2026-10-01T08:00", service_ids=[x["portal"].id], status="CLOSED",
                              resolved_at="2026-10-01T12:00")
    statuses = {d["status"] for d in svc.incident_deadlines(inc.id, now=NOW)["deadlines"]}
    assert "overdue" not in statuses and "not_sent" in statuses
    assert svc.incident_overview(now=NOW)["open"] == []


def test_overview_lists_open_deadlines_soonest_first(svc):
    x = build(svc)
    svc.create_incident("A", "2026-11-27T10:00", service_ids=[x["hub"].id])
    svc.create_incident("B", "2026-11-27T07:00", supplier_ids=[x["cloud"].id], nis2_significant=True)
    o = svc.incident_overview(now=NOW)
    assert len(o["open"]) == 2 and o["unassessed"] == 1 and o["overdue"] == 1  # Bank: 4 h from 07:00
    dues = [d["due"] for d in o["deadlines"]]
    assert dues == sorted(dues)


def test_validation_and_references(svc):
    x = build(svc)
    with pytest.raises(ValidationError):
        svc.create_incident("Bad time", "yesterday")
    with pytest.raises(ValidationError):
        svc.create_incident("Backwards", "2026-11-27T10:00", resolved_at="2026-11-26T10:00")
    inc = svc.create_incident("  Spaces  ", "2026-11-27 10:00:59", supplier_ids=[x["dc"].id])
    assert inc.title == "Spaces" and inc.detected_at == "2026-11-27T10:00"
    svc.delete_supplier(x["dc"].id)
    assert svc.get_incident(inc.id).supplier_ids == []


def test_lei_check():
    assert lei_is_valid(LEI_US) and lei_is_valid("5493001KJTIIGC8Y1R12")
    assert not lei_is_valid("5493001KJTIIGC8Y1R13") and not lei_is_valid("SHORT")


def test_lei_is_validated_and_normalised(svc):
    with pytest.raises(ValidationError):
        svc.create_supplier("X", lei="5493001KJTIIGC8Y1R13")
    assert svc.create_supplier("Y", lei="5493001kjtiigc8y1r12").lei == "5493001KJTIIGC8Y1R12"
    hq = svc.create_business_unit("Z")
    with pytest.raises(ValidationError):
        svc.update_business_unit(hq.id, lei="nope")


def test_dora_extract_ranks_and_gaps(svc):
    x = build(svc)
    d = svc.dora_extract(x["bank"].id)
    assert d["provider"]["lei"] == LEI_US
    assert [(r["rank"], r["provider"], r["recipient"]) for r in d["supply_chain"]] == [
        (1, "Us", "Bank"), (2, "Cloud", "Us"), (3, "DC", "Cloud")]
    assert {p["name"]: p["rank"] for p in d["providers"]} == {"Us": 1, "Cloud": 2, "DC": 3}
    assert d["gaps"] == []  # DC has a company number, the service a type and locations
    town = svc.dora_extract(x["town"].id)
    assert any("Hub" in g and "ICT service type" in g for g in town["gaps"])
    content = svc.export_dora_extract(x["bank"].id)
    from openpyxl import load_workbook
    import io
    wb = load_workbook(io.BytesIO(content))
    assert "Supply chain (B_05.02)" in wb.sheetnames and wb["Providers (B_05.01)"].max_row == 4


def test_excel_round_trip_keeps_incidents(svc):
    x = build(svc)
    inc = svc.create_incident("Outage", "2026-11-27T08:00", service_ids=[x["hub"].id], nis2_significant=None,
                              notifications={"customer:" + x["other"].id: "2026-11-27T09:00"})
    preview = svc.import_excel(svc.export_excel(), dry_run=True)
    assert preview["changes"]["incident"]["unchanged"] == 1
    empty = RiskService(MemoryRepository())
    empty.import_excel(svc.export_excel())
    copy = empty.get_incident(inc.id)
    assert copy.nis2_significant is None and copy.notifications == inc.notifications


def test_add_month_end_of_month():
    assert add_month(datetime(2026, 1, 31, 8)) == datetime(2026, 2, 28, 8)
    assert add_month(datetime(2026, 12, 15)) == datetime(2027, 1, 15)


def test_api():
    svc = RiskService(MemoryRepository())
    x = build(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    r = client.post("/api/incidents", json={"title": "Down", "detected_at": "2026-11-27T08:00",
                                            "service_ids": [x["portal"].id], "nis2_significant": True})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    d = client.get(f"/api/incidents/{iid}/deadlines").json()
    assert {"nis2:early_warning", f"customer:{x['bank'].id}"} <= {k["key"] for k in d["deadlines"]}
    r = client.post(f"/api/incidents/{iid}/notifications", json={"key": "nis2:early_warning"})
    assert r.status_code == 200 and "nis2:early_warning" in r.json()["notifications"]
    assert client.get("/api/incidents/overview").json()["open"][0]["title"] == "Down"
    assert client.get(f"/api/customers/{x['bank'].id}/dora").json()["provider"]["name"] == "Us"
    r = client.get(f"/api/customers/{x['bank'].id}/dora.xlsx")
    assert r.status_code == 200 and 'filename="dora-Bank.xlsx"' in r.headers["content-disposition"]
    assert client.patch(f"/api/incidents/{iid}", json={"status": "CLOSED"}).json()["status"] == "CLOSED"
    assert client.delete(f"/api/incidents/{iid}").status_code == 204
