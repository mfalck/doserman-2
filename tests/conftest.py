"""
Shared fixtures. Every test that uses `repo` or `svc` runs once per backend.

PostgreSQL and Neo4j tests run only when a test database is configured
(the CI workflow starts both as services):
  RISKMESH_TEST_POSTGRES_URL   e.g. postgresql+psycopg://riskmesh:pw@localhost:5432/riskmesh_test
  RISKMESH_TEST_NEO4J_URI, RISKMESH_TEST_NEO4J_USER, RISKMESH_TEST_NEO4J_PASSWORD
WARNING: these tests delete all RiskMesh data in those databases.
"""

import os
import time

import pytest

from riskmesh.services import RiskService
from riskmesh.storage import JsonRepository, MemoryRepository

BACKENDS = ["memory", "json", "sql", "postgres", "neo4j"]


@pytest.fixture(params=BACKENDS)
def repo(request, tmp_path):
    kind = request.param
    if kind == "memory":
        r = MemoryRepository()
    elif kind == "json":
        r = JsonRepository(str(tmp_path / "data.json"))
    elif kind == "sql":
        from riskmesh.storage.sql_store import SqlRepository
        r = SqlRepository(f"sqlite:///{tmp_path / 'data.db'}")
    elif kind == "postgres":
        url = os.environ.get("RISKMESH_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("set RISKMESH_TEST_POSTGRES_URL to test PostgreSQL")
        from riskmesh.storage.sql_store import SqlRepository
        r = SqlRepository(url)
    else:
        uri = os.environ.get("RISKMESH_TEST_NEO4J_URI")
        if not uri:
            pytest.skip("set RISKMESH_TEST_NEO4J_URI to test the Neo4j backend")
        from riskmesh.storage.neo4j_store import Neo4jRepository
        r = Neo4jRepository(uri, os.environ.get("RISKMESH_TEST_NEO4J_USER", "neo4j"),
                            os.environ["RISKMESH_TEST_NEO4J_PASSWORD"])
    r.clear()
    yield r
    r.close()


@pytest.fixture
def svc(repo):
    return RiskService(repo)


# -- a fake OpenID Connect provider (single sign-on tests) -----------------------------

ISSUER = "https://idp.example.com/tenant/v2.0"


@pytest.fixture
def idp(monkeypatch):
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="k1", use="sig", alg="RS256")
    state = {"claims": {}}

    def http_get(self, url):
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISSUER, "authorization_endpoint": "https://idp.example.com/authorize",
                    "token_endpoint": "https://idp.example.com/token", "jwks_uri": "https://idp.example.com/keys"}
        return {"keys": [jwk]}

    def http_post(self, url, data):
        assert data["code"] == "good-code" and data["code_verifier"]
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "rm-client", "sub": "user-123", "iat": now, "exp": now + 300,
                  "nonce": state["nonce"], "preferred_username": "martin@example.com", "name": "Martin",
                  **state["claims"]}
        return {"id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})}

    from riskmesh.presentation import oidc
    monkeypatch.setattr(oidc.OidcClient, "_http_get", http_get)
    monkeypatch.setattr(oidc.OidcClient, "_http_post", http_post)
    return state
