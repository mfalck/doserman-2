"""Customer assurance report (data and printable page)."""

from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain import audit
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.services.demo import seed_demo
from riskmesh.storage import MemoryRepository


def bank(svc):
    return next(c for c in svc.list_customers() if "DORA" in c.regulations)


def test_report_content_and_fingerprint(svc):
    seed_demo(svc)
    r = svc.customer_report(bank(svc).id)
    assert r["customer"]["name"] == "East Savings Bank" and r["provider"]["lei"]
    assert [s["name"] for s in r["services"]] == ["Citizen self-service portal"]
    assert r["supply_chain"][0]["rank"] == 1 and {x["provider"] for x in r["supply_chain"]} >= {"Hyperscale DC GmbH"}
    assert {x["name"] for x in r["risks"]} >= {"Database outage", "Ransomware attack"}
    assert r["penalty_exposure"]["mean"] > 0
    assert any(o["name"].startswith("Citizen portal") for o in r["objectives"])
    # the demo's running incident hits the bank, its 4 h notification is overdue
    assert r["incidents"] and r["incidents"][-1]["notification"] == "overdue"
    body = {k: v for k, v in r.items() if k != "report_hash"}
    assert r["report_hash"] == audit.sha256(body)
    assert r["integrity"]["audit_seq"] > 0


def test_customer_without_dora_has_no_supply_chain_table(svc):
    seed_demo(svc)
    town = next(c for c in svc.list_customers() if c.name == "North Municipality")
    r = svc.customer_report(town.id)
    assert r["supply_chain"] is None and r["suppliers"]


def test_html_is_escaped_and_printable():
    svc = RiskService(MemoryRepository())
    hq = svc.create_business_unit("HQ", organization_type="COMPANY")
    s = svc.create_service(hq.id, "Portal <b>")
    c = svc.create_customer("Evil <script>alert(1)</script>", service_ids=[s.id])
    page = svc.customer_report_html(c.id)
    assert "<script>alert" not in page and "Evil &lt;script&gt;" in page and "Portal &lt;b&gt;" in page
    assert "@media print" in page and "SHA-256" in page


def test_api():
    svc = RiskService(MemoryRepository())
    seed_demo(svc)
    client = TestClient(create_app(svc, Settings(backend="memory", api_key=None)))
    cid = bank(svc).id
    assert client.get(f"/api/customers/{cid}/report").json()["customer"]["name"] == "East Savings Bank"
    r = client.get(f"/api/customers/{cid}/report.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Assurance report: East Savings Bank" in r.text
