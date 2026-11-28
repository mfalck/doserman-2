"""Workspaces: independent analyses with their own storage, users and audit log."""

from urllib.parse import parse_qs, urlparse

import pytest
from conftest import ISSUER
from fastapi.testclient import TestClient

from riskmesh.bootstrap import Workspaces, make_service
from riskmesh.config import Settings, load_settings
from riskmesh.domain.errors import NotFoundError
from riskmesh.presentation.api import create_app
from riskmesh.presentation.cli import main

PW = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("RISKMESH_CONFIG", "RISKMESH_BACKEND", "RISKMESH_JSON_PATH", "RISKMESH_SQL_URL",
                 "RISKMESH_WORKSPACE", "RISKMESH_API_KEY", "RISKMESH_AUTH_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


CONFIG = '''
[storage]
title = "Our own"
backend = "json"
json_path = "own.json"

[workspaces.acme]
title = "ACME A/S"
json_path = "acme.json"

[workspaces.bank]
title = "Bank"
backend = "sql"
sql_url = "sqlite:///bank.db"
'''


def write(tmp_path, text=CONFIG):
    p = tmp_path / "cfg" / "riskmesh.toml"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# -- configuration ------------------------------------------------------------------

def test_storage_is_the_default_workspace_and_tables_add_more(tmp_path):
    s = load_settings(str(write(tmp_path)))
    assert s.workspace == "default" and s.workspace_ids() == ["default", "acme", "bank"]
    assert [w["title"] for w in s.workspace_info()] == ["Our own", "ACME A/S", "Bank"]
    acme = s.for_workspace("acme")
    assert (acme.workspace, acme.workspace_title, acme.backend) == ("acme", "ACME A/S", "json")
    assert acme.json_path == str((tmp_path / "cfg" / "acme.json").resolve())  # relative to the file
    bank = s.for_workspace("bank")
    assert bank.backend == "sql" and bank.sql_url.endswith("cfg/bank.db")
    back = bank.for_workspace("default")  # and back again
    assert (back.backend, back.json_path) == ("json", str((tmp_path / "cfg" / "own.json").resolve()))
    with pytest.raises(KeyError):
        s.for_workspace("nope")


def test_environment_picks_the_workspace(tmp_path, monkeypatch):
    cfg = str(write(tmp_path))
    monkeypatch.setenv("RISKMESH_WORKSPACE", "bank")
    s = load_settings(cfg)
    assert (s.workspace, s.backend) == ("bank", "sql")
    monkeypatch.setenv("RISKMESH_WORKSPACE", "nope")
    with pytest.raises(ValueError, match="unknown workspace"):
        load_settings(cfg)


@pytest.mark.parametrize("extra, message", [
    ('[workspaces.copy]\njson_path = "acme.json"\n', "same storage"),
    ('[workspaces.own]\njson_path = "own.json"\n', "same storage"),
    ('[workspaces.default]\njson_path = "x.json"\n', "reserved"),
    ('[workspaces.Big]\njson_path = "x.json"\n', "lower-case"),
    ('[workspaces.x]\njsonpath = "x.json"\n', "unknown keys"),
    ('[workspaces.x]\nbackend = "sql"\n', "needs sql_url"),
    ('[workspaces.x]\nbackend = "neo4j"\n', "neo4j.database"),
    ('[workspaces.x]\njson_path = "x.json"\nrole_map = { "G" = "boss" }\n', "unknown role"),
])
def test_workspaces_never_share_storage_and_are_checked(tmp_path, extra, message):
    with pytest.raises(ValueError, match=message):
        load_settings(str(write(tmp_path, CONFIG + extra)))


# -- services -----------------------------------------------------------------------

def test_each_workspace_has_its_own_data_and_audit_log(tmp_path):
    ws = Workspaces(load_settings(str(write(tmp_path))), actor="tester")
    try:
        ws.service("acme").create_business_unit("ACME IT")
        ws.service("bank").create_business_unit("Bank IT")
        names = {w: [u.name for u in ws.service(w).list_business_units()] for w in ws.ids()}
        assert names == {"default": [], "acme": ["ACME IT"], "bank": ["Bank IT"]}
        assert ws.service("acme") is ws.service("acme")  # opened once
        assert len(ws.service("acme").list_audit()) == 1 and ws.service("default").list_audit() == []
        with pytest.raises(NotFoundError):
            ws.service("nope")
    finally:
        ws.close()
    # it was stored where the configuration says
    acme = make_service(load_settings(str(write(tmp_path))).for_workspace("acme"))
    with acme.repo:
        assert [u.name for u in acme.list_business_units()] == ["ACME IT"]


# -- API ----------------------------------------------------------------------------

def memory_settings(**extra):
    workspaces = {"default": {"title": "Default", "backend": "memory"},
                  "acme": {"title": "ACME", "backend": "memory"},
                  "bank": {"title": "Bank", "backend": "memory"}}
    return Settings(backend="memory", workspaces=workspaces, **extra)


def test_the_header_picks_the_workspace():
    with TestClient(create_app(settings=memory_settings())) as c:
        assert c.post("/api/business-units", json={"name": "ACME IT"}, headers={"X-Workspace": "acme"}
                      ).status_code == 201
        names = lambda **kw: [u["name"] for u in c.get("/api/business-units", **kw).json()]
        assert names(headers={"X-Workspace": "acme"}) == ["ACME IT"]
        assert names(params={"workspace": "acme"}) == ["ACME IT"]  # for links and downloads
        assert names() == [] and names(headers={"X-Workspace": "bank"}) == []
        r = c.get("/api/business-units", headers={"X-Workspace": "nope"})
        assert r.status_code == 404 and "Unknown workspace" in r.json()["detail"]
        assert c.get("/health", headers={"X-Workspace": "bank"}).json()["workspace"] == "bank"
        cfg = c.get("/api/auth/config", headers={"X-Workspace": "acme"}).json()
        assert cfg["workspace"] == {"id": "acme", "title": "ACME"} and cfg["default_workspace"] == "default"
        assert [w["id"] for w in cfg["workspaces"]] == ["default", "acme", "bank"]
    with TestClient(create_app(settings=memory_settings(list_workspaces=False))) as c:
        assert c.get("/api/auth/config").json()["workspaces"] == []  # names not shown before login


def test_a_login_only_works_in_its_own_workspace():
    app = create_app(settings=memory_settings(auth_mode="local"))
    with TestClient(app) as c:
        acme = {"X-Workspace": "acme"}
        svc = app.state.workspaces  # each workspace has its own users: acme's first admin
        svc.service("acme").create_user("anna", "admin", PW)
        assert c.post("/api/auth/login", json={"username": "anna", "password": PW}).status_code == 401
        r = c.post("/api/auth/login", json={"username": "anna", "password": PW}, headers=acme)
        assert r.status_code == 200
        token = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c.get("/api/auth/me", headers={**token, **acme}).json()["workspace"] == "acme"
        assert c.get("/api/risks", headers={**token, **acme}).status_code == 200
        assert c.get("/api/risks", headers=token).status_code == 401  # not valid in the default workspace
        assert c.get("/api/risks", headers={**token, "X-Workspace": "bank"}).status_code == 401
        assert svc.service("default").list_users() == []


def sso(c, idp, workspace, **claims):
    idp["claims"] = claims
    r = c.get(f"/api/auth/oidc/login?workspace={workspace}", follow_redirects=False)
    if r.status_code != 302:
        return r
    q = parse_qs(urlparse(r.headers["location"]).query)
    idp["nonce"] = q["nonce"][0]
    # the provider calls back without our header
    return c.get(f"/api/auth/oidc/callback?code=good-code&state={q['state'][0]}", follow_redirects=False)


def test_single_sign_on_into_a_workspace_uses_its_rules(idp):
    settings = memory_settings(auth_mode="oidc", oidc_issuer=ISSUER, oidc_client_id="rm-client",
                               oidc_role_map={"RiskMesh.Admin": "admin"}, oidc_default_role="viewer")
    settings.workspaces["acme"].update(oidc_role_map={"ACME.Editors": "editor"}, oidc_default_role=None)
    settings.workspaces["bank"].update(workspace_sso=False)
    with TestClient(create_app(settings=settings)) as c:
        cfg = c.get("/api/auth/config", headers={"X-Workspace": "acme"}).json()
        assert cfg["oidc_login_url"] == "/api/auth/oidc/login?workspace=acme"
        location = sso(c, idp, "acme", roles=["ACME.Editors"]).headers["location"]
        fragment = parse_qs(location.split("#", 1)[1])
        assert fragment["workspace"] == ["acme"]
        h = {"Authorization": f"Bearer {fragment['token'][0]}", "X-Workspace": "acme"}
        assert c.get("/api/auth/me", headers=h).json()["role"] == "editor"
        assert c.get("/api/auth/me", headers={"Authorization": h["Authorization"]}).status_code == 401
        # the global admin group means nothing in acme, and acme has no default role
        assert "#error=" in sso(c, idp, "acme", roles=["RiskMesh.Admin"]).headers["location"]
        # no single sign-on into the bank workspace
        assert c.get("/api/auth/config", headers={"X-Workspace": "bank"}).json()["oidc"] is False
        assert sso(c, idp, "bank").status_code == 404


# -- CLI ----------------------------------------------------------------------------

def test_cli_lists_and_works_in_a_workspace(tmp_path, capsys):
    cfg = str(write(tmp_path))
    assert main(["--config", cfg, "workspaces"]) == 0
    out = capsys.readouterr().out
    assert "* default" in out and "  acme" in out and "ACME A/S" in out and "sql" in out
    assert main(["--config", cfg, "-w", "acme", "demo"]) == 0
    capsys.readouterr()
    assert main(["--config", cfg, "-w", "acme", "tree"]) == 0
    assert capsys.readouterr().out.strip()
    assert (tmp_path / "cfg" / "acme.json").exists() and not (tmp_path / "cfg" / "own.json").exists()
    assert main(["--config", cfg, "-w", "nope", "tree"]) == 1
