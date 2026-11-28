"""Login, roles, tokens and single sign-on through the API."""

import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from riskmesh.config import Settings
from riskmesh.domain.errors import AuthenticationError, ConflictError, ValidationError
from riskmesh.presentation.api import create_app, required_role
from riskmesh.services import RiskService
from riskmesh.services.users import check_password, hash_password
from riskmesh.storage import MemoryRepository

PW = "correct horse battery"


def make(mode="local", **extra):
    svc = RiskService(MemoryRepository())
    admin = svc.create_user("admin", "admin", PW)
    svc.create_user("ed", "editor", PW)
    svc.create_user("vi", "viewer", PW)
    settings = Settings(backend="memory", auth_mode=mode, **extra)
    return svc, admin, TestClient(create_app(svc, settings))


def login(client, user, pw=PW):
    r = client.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# -- service level -----------------------------------------------------------------

def test_password_hashing():
    h = hash_password("secret-secret")
    assert h.startswith("scrypt$") and "secret" not in h
    assert check_password("secret-secret", h) and not check_password("wrong-wrong", h)
    assert hash_password("secret-secret") != h  # random salt


def test_user_rules_and_audit_masking():
    svc = RiskService(MemoryRepository())
    with pytest.raises(ValidationError):
        svc.create_user("x", "admin", "short")
    a = svc.create_user("admin", "admin", PW)
    with pytest.raises(ConflictError):
        svc.create_user("admin", "viewer", PW)
    with pytest.raises(ConflictError):
        svc.update_user(a.id, role="editor")  # last admin
    svc.set_password(a.id, "another long password")
    entries = svc.list_audit()
    assert all(e.changes.get("password_hash", ["***", "***"])[1] in ("***", None) for e in entries)
    assert "scrypt$" not in str([e.changes for e in entries])
    assert svc.verify_audit()["ok"]


def test_tokens_expire_revoke_and_deactivation():
    svc = RiskService(MemoryRepository())
    u = svc.create_user("ann", "editor", PW)
    tok, rec = svc.issue_token(u.id, hours=1)
    assert svc.resolve_token(tok).username == "ann"
    assert svc.resolve_token("rm_wrong") is None
    svc.revoke_token(rec.id)
    assert svc.resolve_token(tok) is None
    tok2, _ = svc.issue_token(u.id, hours=-1)  # already expired
    assert svc.resolve_token(tok2) is None
    tok3, _ = svc.issue_token(u.id, "api", "script")
    svc.update_user(u.id, active=False)
    assert svc.resolve_token(tok3) is None
    with pytest.raises(AuthenticationError):
        svc.authenticate("ann", PW)


def test_reset_keeps_users():
    svc = RiskService(MemoryRepository())
    svc.create_user("admin", "admin", PW)
    svc.reset_all()
    assert [u.username for u in svc.list_users()] == ["admin"]
    assert svc.verify_audit()["ok"]


def test_access_policy():
    assert required_role("GET", "/api/risks", {}) == "viewer"
    assert required_role("POST", "/api/risks", {}) == "editor"
    assert required_role("POST", "/api/simulations/compare", {}) == "viewer"
    assert required_role("POST", "/api/drivers/what-if", {}) == "viewer"
    assert required_role("POST", "/api/drivers", {}) == "editor"
    assert required_role("PATCH", "/api/runs/x", {}) == "editor"
    assert required_role("POST", "/api/import", {}) == "admin"
    assert required_role("POST", "/api/import", {"replace": "false"}) == "editor"
    assert required_role("GET", "/api/users", {}) == "admin"


# -- API ------------------------------------------------------------------------------

def test_requests_need_a_token_and_the_right_role():
    _, _, c = make()
    assert c.get("/api/risks").status_code == 401
    assert c.get("/health").status_code == 200
    assert c.get("/api/auth/config").json() == {"mode": "local", "local": True, "oidc": False,
                                                 "oidc_login_url": None}
    viewer, editor, admin = login(c, "vi"), login(c, "ed"), login(c, "admin")
    assert c.get("/api/risks", headers=viewer).status_code == 200
    assert c.post("/api/persons", json={"name": "X"}, headers=viewer).status_code == 403
    r = c.post("/api/persons", json={"name": "X"}, headers=editor)
    assert r.status_code == 201
    assert c.get("/api/users", headers=editor).status_code == 403
    assert len(c.get("/api/users", headers=admin).json()) == 3
    entry = c.get("/api/audit-log?limit=1", headers=viewer).json()[0]
    assert entry["user_id"] == "ed"  # from the token, not from a header the client could choose


def test_x_user_id_header_cannot_impersonate():
    _, _, c = make()
    h = {**login(c, "ed"), "X-User-Id": "admin"}
    c.post("/api/persons", json={"name": "Y"}, headers=h)
    assert c.get("/api/audit-log?limit=1", headers=h).json()[0]["user_id"] == "ed"


def test_login_failures_are_limited_and_vague():
    _, _, c = make()
    r = c.post("/api/auth/login", json={"username": "nobody", "password": "x" * 10})
    assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password."
    for _ in range(5):
        c.post("/api/auth/login", json={"username": "ed", "password": "wrong password"})
    assert c.post("/api/auth/login", json={"username": "ed", "password": PW}).status_code == 429


def test_logout_password_change_and_api_tokens():
    _, _, c = make()
    h = login(c, "ed")
    assert c.get("/api/auth/me", headers=h).json()["username"] == "ed"
    assert c.post("/api/auth/password", json={"current_password": PW, "new_password": "a new long password"},
                  headers=h).status_code == 200
    assert c.get("/api/auth/me", headers=h).status_code == 401  # sessions ended by the change
    h = login(c, "ed", "a new long password")
    c.post("/api/auth/logout", headers=h)
    assert c.get("/api/risks", headers=h).status_code == 401

    admin = login(c, "admin")
    users = {u["username"]: u for u in c.get("/api/users", headers=admin).json()}
    assert "password_hash" not in users["vi"]
    r = c.post(f"/api/users/{users['vi']['id']}/tokens", json={"name": "report script"}, headers=admin)
    token = r.json()["token"]
    assert c.get("/api/risks", headers={"X-API-Key": token}).status_code == 200
    assert c.post("/api/persons", json={"name": "Z"}, headers={"X-API-Key": token}).status_code == 403
    c.delete(f"/api/tokens/{r.json()['id']}", headers=admin)
    assert c.get("/api/risks", headers={"X-API-Key": token}).status_code == 401


def test_bootstrap_admin_from_environment(monkeypatch):
    monkeypatch.setenv("RISKMESH_ADMIN_USER", "root")
    monkeypatch.setenv("RISKMESH_ADMIN_PASSWORD", PW)
    svc = RiskService(MemoryRepository())
    c = TestClient(create_app(svc, Settings(backend="memory", auth_mode="local")))
    assert c.post("/api/auth/login", json={"username": "root", "password": PW}).status_code == 200


# -- single sign-on with a fake provider --------------------------------------------------

ISSUER = "https://idp.example.com/tenant/v2.0"


@pytest.fixture
def idp(monkeypatch):
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


def sso_login(c, idp, **claims):
    idp["claims"] = claims
    r = c.get("/api/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"]
    idp["nonce"] = q["nonce"][0]
    r = c.get(f"/api/auth/oidc/callback?code=good-code&state={q['state'][0]}", follow_redirects=False)
    return r.headers["location"]


def test_single_sign_on_creates_user_with_mapped_role(idp):
    svc, _, c = make("local+oidc", oidc_issuer=ISSUER, oidc_client_id="rm-client",
                     oidc_role_map={"RiskMesh.Editor": "editor", "RiskMesh.Admin": "admin"})
    location = sso_login(c, idp, roles=["RiskMesh.Editor"])
    assert location.startswith("http://localhost:8080/login#token=")
    h = {"Authorization": f"Bearer {location.split('#token=')[1]}"}
    me = c.get("/api/auth/me", headers=h).json()
    assert (me["username"], me["role"], me["auth_source"]) == ("martin@example.com", "editor", "oidc")
    # role follows the provider on the next login
    sso_login(c, idp, roles=["RiskMesh.Admin"])
    assert [u.role for u in svc.list_users() if u.auth_source == "oidc"] == ["admin"]


def test_single_sign_on_rejects_bad_tokens_and_unmapped_users(idp):
    _, _, c = make("oidc", oidc_issuer=ISSUER, oidc_client_id="rm-client", oidc_default_role=None,
                   oidc_role_map={"RiskMesh.Viewer": "viewer"})
    assert "#error=" in sso_login(c, idp, roles=["Something.Else"])  # no RiskMesh role
    assert "#error=" in sso_login(c, idp, roles=["RiskMesh.Viewer"], aud="other-app")  # wrong audience
    r = c.get("/api/auth/oidc/callback?code=good-code&state=forged", follow_redirects=False)
    assert "#error=" in r.headers["location"]
    assert c.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 404  # no passwords


def test_closing_periods_needs_admin():
    assert required_role("POST", "/api/periods/2026-Q4/close", {}) == "admin"
    assert required_role("POST", "/api/periods/2026-Q4/reopen", {}) == "admin"
    assert required_role("GET", "/api/reports/compare", {}) == "viewer"
    assert required_role("POST", "/api/audit-log/anchor", {}) == "admin"
