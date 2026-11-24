"""REST API on top of an in-memory backend."""

import pytest
from fastapi.testclient import TestClient

from quanterm.config import Settings
from quanterm.presentation.api import create_app
from quanterm.services import RiskService
from quanterm.storage import MemoryRepository


@pytest.fixture
def client():
    svc = RiskService(MemoryRepository())
    with TestClient(create_app(svc, Settings(backend="memory", api_key=None))) as c:
        yield c


def test_crud_flow_and_simulation(client):
    bu = client.post("/api/business-units", json={"name": "IT"}).json()
    child = client.post("/api/business-units", json={"name": "Dev", "parent_id": bu["id"]}).json()
    asset = client.post("/api/assets", json={"name": "DB", "asset_type": "virtual",
                                             "business_unit_id": bu["id"]}).json()
    person = client.post("/api/persons", json={"name": "Jane"}).json()
    assert client.put(f"/api/business-units/{bu['id']}/risk-owner",
                      json={"person_id": person["id"]}).json()["risk_owner_id"] == person["id"]

    r = client.post("/api/risks", json={
        "name": "Outage", "business_unit_id": child["id"], "asset_ids": [asset["id"]],
        "frequency": {"type": "poisson", "rate": 2}, "impact": {"dist": "fixed", "value": 100},
    })
    assert r.status_code == 201, r.text
    risk = r.json()

    patched = client.patch(f"/api/risks/{risk['id']}", json={"category": "operational"}).json()
    assert patched["category"] == "operational" and patched["frequency"]["rate"] == 2

    tree = client.get("/api/business-units/tree").json()
    assert tree[0]["children"][0]["name"] == "Dev"

    sim = client.post("/api/simulations", json={"n_trials": 2000, "seed": 1}).json()
    assert sim["total"]["mean"] == pytest.approx(200, rel=0.1)  # 2 events x 100

    assert client.get("/api/dashboard").json()["counts"]["risks"] == 1
    assert client.delete(f"/api/risks/{risk['id']}").status_code == 204


def test_error_mapping(client):
    assert client.get("/api/risks/missing").status_code == 404
    client.post("/api/business-units", json={"name": "A"})
    assert client.post("/api/business-units", json={"name": "A"}).status_code == 409
    bu = client.get("/api/business-units").json()[0]
    bad = client.post("/api/risks", json={"name": "x", "business_unit_id": bu["id"],
                                          "frequency": {"type": "poisson", "rate": -1},
                                          "impact": {"dist": "fixed", "value": 1}})
    assert bad.status_code == 422
    assert client.post("/api/persons", json={"name": "A", "unknown": 1}).status_code == 422


def test_patch_null_clears_parent(client):
    a = client.post("/api/business-units", json={"name": "A"}).json()
    b = client.post("/api/business-units", json={"name": "B", "parent_id": a["id"]}).json()
    out = client.patch(f"/api/business-units/{b['id']}", json={"parent_id": None}).json()
    assert out["parent_id"] is None


def test_api_key_is_enforced():
    app = create_app(RiskService(MemoryRepository()), Settings(backend="memory", api_key="secret"))
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200  # health stays open
        assert c.get("/api/persons").status_code == 401
        assert c.get("/api/persons", headers={"X-API-Key": "secret"}).status_code == 200


def test_cors_allows_configured_origin():
    settings = Settings(backend="memory", api_key=None, cors_origins=["https://doserman.lovable.app"])
    with TestClient(create_app(RiskService(MemoryRepository()), settings)) as c:
        r = c.options("/api/persons", headers={"Origin": "https://doserman.lovable.app",
                                               "Access-Control-Request-Method": "GET"})
        assert r.headers.get("access-control-allow-origin") == "https://doserman.lovable.app"
