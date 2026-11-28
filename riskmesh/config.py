"""
Configuration: a riskmesh.toml file, overridden by environment variables.

Where the file is looked for (first found wins):
  1. --config PATH on the command line (or the GUI's open dialog)
  2. the RISKMESH_CONFIG environment variable
  3. riskmesh.toml in the current folder
  4. the user's config folder: %APPDATA%\\RiskMesh\\riskmesh.toml on Windows,
     ~/.config/riskmesh/riskmesh.toml elsewhere

Relative paths in the file (json_path, sqlite URLs) are relative to the file.
Secrets (Neo4j password, API key) should not be written in the file: name an
environment variable instead (password_env / api_key_env).

Environment variables always win:
  RISKMESH_BACKEND, RISKMESH_JSON_PATH, RISKMESH_SQL_URL, RISKMESH_NEO4J_URI,
  RISKMESH_NEO4J_USER, RISKMESH_NEO4J_PASSWORD, RISKMESH_NEO4J_DATABASE,
  RISKMESH_API_KEY, RISKMESH_CORS_ORIGINS (comma-separated),
  RISKMESH_WORKSPACE (which workspace the CLI and desktop app open)

Workspaces: [storage] is the workspace "default"; each [workspaces.<id>]
table is another, independent analysis with its own storage (file, database
or Neo4j database), its own users and its own audit log. No two workspaces
may share a storage location.

`riskmesh config init` writes a commented template; `riskmesh config show`
prints the effective settings.
"""

import os
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

FILE_NAME = "riskmesh.toml"
BACKENDS = ("json", "sql", "neo4j", "memory")
ENGINES = ("analytic", "monte_carlo")
AUTH_MODES = ("none", "local", "oidc", "local+oidc")

TEMPLATE = '''# RiskMesh configuration. Environment variables (RISKMESH_*) override these values.

[storage]
title = "Default"                   # name of this workspace in the clients
backend = "json"                    # json | sql | neo4j | memory
json_path = "riskmesh_data.json"    # json backend; relative to this file
sql_url = "sqlite:///riskmesh.db"   # sql backend, e.g. postgresql+psycopg://user@host/riskmesh

[storage.neo4j]
uri = "neo4j://localhost:7687"
user = "neo4j"
password_env = "RISKMESH_NEO4J_PASSWORD"   # name of the environment variable holding the password
database = ""

[api]
host = "127.0.0.1"
port = 8000
api_key_env = "RISKMESH_API_KEY"    # if that variable is set, requests need header X-API-Key
cors_origins = []                   # e.g. ["https://riskmesh.lovable.app", "http://localhost:8080"]

[analysis]
engine = "analytic"                 # analytic | monte_carlo
n_trials = 50000                    # Monte Carlo only
seed = 42                           # Monte Carlo only

[scoring]
# 1-5 likelihood from P(at least one event per year); 1-5 impact from the median loss per event
likelihood_bands = [0.05, 0.20, 0.50, 0.80]
impact_bands = [50000, 250000, 1000000, 5000000]
currency = "DKK"

[nis2]
# A single incident above this loss counts as significant (NIS2 art. 23(3); for some digital
# providers Implementing Regulation (EU) 2024/2690: EUR 500,000 or 5% of turnover). In the currency above.
significant_loss = 3750000
# significant_outage_hours = 24      # optionally also an outage longer than this

[user]
name = ""                           # name recorded in the audit log by the GUI/CLI (default: login name)

[audit]
# Anchoring makes removal of the newest audit entries detectable (see README).
signing_key = ""                    # Ed25519 private key file (riskmesh audit keygen); relative to this file
tsa_url = ""                        # RFC 3161 time-stamp authority, e.g. "https://freetsa.org/tsr" (only a hash is sent)
anchor_on_period_close = true

[auth]
# none       = no login (single user on your own machine; the API is open to anyone who can reach it)
# local      = RiskMesh users with passwords (create the first admin: riskmesh users create-admin)
# oidc       = single sign-on via Microsoft Entra ID or another OpenID Connect provider
# local+oidc = both
mode = "none"
session_hours = 12

[auth.oidc]
issuer = ""                         # e.g. "https://login.microsoftonline.com/<tenant-id>/v2.0"
client_id = ""
client_secret_env = "RISKMESH_OIDC_CLIENT_SECRET"
redirect_uri = "http://127.0.0.1:8000/api/auth/oidc/callback"   # must be registered at the provider
frontend_url = "http://localhost:8080"                           # where users land after signing in
scopes = ["openid", "profile", "email"]
username_claim = "preferred_username"
role_claim = "roles"                # Entra ID app roles; use "groups" for group ids
role_map = { "RiskMesh.Admin" = "admin", "RiskMesh.Editor" = "editor", "RiskMesh.Viewer" = "viewer" }
default_role = "viewer"             # role for users without a mapped claim; "" = refuse them

# Workspaces: independent analyses (e.g. one per engagement), each with its own storage, users
# and audit log. [storage] above is the workspace "default". Choose one with --workspace or
# RISKMESH_WORKSPACE; the web app sends the header X-Workspace.
# [workspaces]
# list = true                       # false: the login page does not show the workspace names
#
# [workspaces.acme]
# title = "ACME A/S - NIS2 2026"
# backend = "json"
# json_path = "acme.json"
# sso = true                        # false: no single sign-on into this workspace
# role_map = { "RiskMesh.ACME" = "editor" }   # replaces [auth.oidc] role_map here
# default_role = ""                 # replaces [auth.oidc] default_role here ("" = only mapped users)
#
# [workspaces.bank]
# title = "Bank - DORA"
# backend = "sql"
# sql_url = "sqlite:///bank.db"
'''

WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
STORAGE_FIELDS = ("backend", "json_path", "sql_url", "neo4j_uri", "neo4j_user", "neo4j_password", "neo4j_database")


@dataclass
class Settings:
    backend: str = "json"
    json_path: str = "riskmesh_data.json"
    sql_url: str = "sqlite:///riskmesh.db"
    neo4j_uri: str = "neo4j://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: Optional[str] = None
    neo4j_database: Optional[str] = None
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_key: Optional[str] = None
    cors_origins: List[str] = field(default_factory=list)
    engine: str = "analytic"
    n_trials: int = 50_000
    seed: Optional[int] = 42
    likelihood_bands: Tuple[float, ...] = (0.05, 0.20, 0.50, 0.80)
    impact_bands: Tuple[float, ...] = (50_000, 250_000, 1_000_000, 5_000_000)
    currency: str = "DKK"
    # NIS2: a single incident above this loss (or longer outage) counts as significant; ~EUR 500,000 in DKK
    significant_loss: Optional[float] = 3_750_000
    significant_outage_hours: Optional[float] = None
    user_name: Optional[str] = None
    audit_signing_key: Optional[str] = None
    audit_tsa_url: Optional[str] = None
    anchor_on_period_close: bool = True
    auth_mode: str = "none"            # none | local | oidc | local+oidc
    session_hours: float = 12
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_redirect_uri: str = "http://127.0.0.1:8000/api/auth/oidc/callback"
    oidc_frontend_url: str = "http://localhost:8080"
    oidc_scopes: Tuple[str, ...] = ("openid", "profile", "email")
    oidc_username_claim: str = "preferred_username"
    oidc_role_claim: str = "roles"
    oidc_role_map: Dict[str, str] = field(default_factory=dict)
    oidc_default_role: Optional[str] = "viewer"
    config_file: Optional[str] = None  # which file was loaded, if any
    # the workspace these storage settings belong to, and all workspaces by id (with "default")
    workspace: str = "default"
    workspace_title: str = "Default"
    workspace_sso: bool = True
    workspaces: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    list_workspaces: bool = True

    def workspace_ids(self) -> List[str]:
        return list(self.workspaces) or [self.workspace]

    def workspace_info(self) -> List[Dict[str, Any]]:
        if not self.workspaces:
            return [{"id": self.workspace, "title": self.workspace_title}]
        return [{"id": wid, "title": ws.get("title") or wid} for wid, ws in self.workspaces.items()]

    def for_workspace(self, workspace_id: str) -> "Settings":
        """The same settings, with the storage (and single sign-on rules) of another workspace."""
        if workspace_id == self.workspace:
            return self
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise KeyError(f"Unknown workspace {workspace_id!r} (configured: {', '.join(self.workspace_ids())})")
        return replace(self, workspace=workspace_id, workspace_title=ws.get("title") or workspace_id,
                       **{k: ws[k] for k in STORAGE_FIELDS + ("workspace_sso", "oidc_role_map", "oidc_default_role")
                          if k in ws})

    @staticmethod
    def _location(ws: Dict[str, Any]) -> Optional[str]:
        backend = ws["backend"]
        if backend == "json":
            return "json:" + os.path.normcase(os.path.abspath(ws["json_path"]))
        if backend == "sql":
            return "sql:" + ws["sql_url"]
        if backend == "neo4j":
            return f"neo4j:{ws['neo4j_uri']}/{ws.get('neo4j_database') or ''}"
        return None  # memory: each one is separate

    @property
    def local_login(self) -> bool:
        return self.auth_mode in ("local", "local+oidc")

    @property
    def oidc_login(self) -> bool:
        return self.auth_mode in ("oidc", "local+oidc")

    def validate(self) -> "Settings":
        if self.backend not in BACKENDS:
            raise ValueError(f"backend must be one of {', '.join(BACKENDS)} (got {self.backend!r})")
        if self.engine not in ENGINES:
            raise ValueError(f"analysis.engine must be one of {', '.join(ENGINES)} (got {self.engine!r})")
        for name in ("likelihood_bands", "impact_bands"):
            bands = list(getattr(self, name))
            if len(bands) != 4 or bands != sorted(bands) or len(set(bands)) != 4:
                raise ValueError(f"scoring.{name} must be 4 increasing numbers (got {bands})")
        if not 100 <= int(self.n_trials) <= 1_000_000:
            raise ValueError("analysis.n_trials must be between 100 and 1,000,000")
        if self.auth_mode not in AUTH_MODES:
            raise ValueError(f"auth.mode must be one of {', '.join(AUTH_MODES)} (got {self.auth_mode!r})")
        if self.oidc_login and not (self.oidc_issuer and self.oidc_client_id):
            raise ValueError("auth.oidc needs issuer and client_id when single sign-on is enabled")
        for role in list(self.oidc_role_map.values()) + ([self.oidc_default_role] if self.oidc_default_role else []):
            if role not in ("viewer", "editor", "admin"):
                raise ValueError(f"auth.oidc: unknown role {role!r} (use viewer, editor or admin)")
        seen: Dict[str, str] = {}
        for wid, ws in self.workspaces.items():
            if not WORKSPACE_ID.match(wid):
                raise ValueError(f"workspace id {wid!r}: use lower-case letters, digits, - and _ (max 40)")
            if ws["backend"] not in BACKENDS:
                raise ValueError(f"workspaces.{wid}.backend must be one of {', '.join(BACKENDS)}")
            for role in list(ws.get("oidc_role_map", {}).values()) + [ws.get("oidc_default_role") or "viewer"]:
                if role not in ("viewer", "editor", "admin"):
                    raise ValueError(f"workspaces.{wid}: unknown role {role!r}")
            where = self._location(ws)
            if where in seen:  # watertight: never two workspaces in one place
                raise ValueError(f"workspaces {seen[where]!r} and {wid!r} use the same storage ({where})")
            if where:
                seen[where] = wid
        if self.workspaces and self.workspace not in self.workspaces:
            raise ValueError(f"Unknown workspace {self.workspace!r} (configured: {', '.join(self.workspaces)})")
        return self

    def public(self) -> Dict[str, Any]:
        """Settings for display, with secrets masked."""
        d = asdict(self)
        for secret in ("neo4j_password", "api_key", "oidc_client_secret"):
            d[secret] = "***" if d[secret] else None
        d["workspaces"] = ", ".join(self.workspace_ids())
        return d


def user_config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "RiskMesh"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "riskmesh"


def find_config(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
        return path
    for candidate in (os.environ.get("RISKMESH_CONFIG"), Path.cwd() / FILE_NAME, user_config_dir() / FILE_NAME):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _resolve_path(value: str, base: Path) -> str:
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())


def _resolve_sql_url(url: str, base: Path) -> str:
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith(prefix + "/") and url != "sqlite:///:memory:":
        path = url[len(prefix):]
        if not Path(path).is_absolute():
            return prefix + _resolve_path(path, base).replace("\\", "/")
    return url


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def load_settings(path: Optional[str] = None) -> Settings:
    """Defaults <- config file <- environment variables."""
    s = Settings()
    config = find_config(path)
    ws_tables: Dict[str, Any] = {}
    base = Path.cwd()
    if config is not None:
        with open(config, "rb") as f:
            try:
                data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise ValueError(f"{config}: {e}") from e
        base = config.parent
        s.config_file = str(config)
        storage = data.get("storage", {})
        neo = storage.get("neo4j", {})
        api = data.get("api", {})
        analysis = data.get("analysis", {})
        scoring = data.get("scoring", {})
        s.backend = storage.get("backend", s.backend)
        s.workspace_title = storage.get("title", s.workspace_title)
        ws_cfg = data.get("workspaces", {})
        s.list_workspaces = bool(ws_cfg.get("list", True))
        ws_tables = {k: v for k, v in ws_cfg.items() if isinstance(v, dict)}
        if "json_path" in storage:
            s.json_path = _resolve_path(storage["json_path"], base)
        if "sql_url" in storage:
            s.sql_url = _resolve_sql_url(storage["sql_url"], base)
        s.neo4j_uri = neo.get("uri", s.neo4j_uri)
        s.neo4j_user = neo.get("user", s.neo4j_user)
        s.neo4j_database = neo.get("database") or None
        s.neo4j_password = neo.get("password") or (_env(neo["password_env"]) if neo.get("password_env") else None)
        s.api_host = api.get("host", s.api_host)
        s.api_port = int(api.get("port", s.api_port))
        s.api_key = api.get("api_key") or (_env(api["api_key_env"]) if api.get("api_key_env") else None)
        s.cors_origins = list(api.get("cors_origins", s.cors_origins))
        s.engine = analysis.get("engine", s.engine)
        s.n_trials = int(analysis.get("n_trials", s.n_trials))
        s.seed = analysis.get("seed", s.seed)
        s.likelihood_bands = tuple(scoring.get("likelihood_bands", s.likelihood_bands))
        s.impact_bands = tuple(scoring.get("impact_bands", s.impact_bands))
        s.currency = scoring.get("currency", s.currency)
        nis2 = data.get("nis2", {})
        s.significant_loss = nis2.get("significant_loss", s.significant_loss)
        s.significant_outage_hours = nis2.get("significant_outage_hours", s.significant_outage_hours)
        s.user_name = data.get("user", {}).get("name") or None
        audit_cfg = data.get("audit", {})
        if audit_cfg.get("signing_key"):
            s.audit_signing_key = _resolve_path(audit_cfg["signing_key"], base)
        s.audit_tsa_url = audit_cfg.get("tsa_url") or None
        s.anchor_on_period_close = bool(audit_cfg.get("anchor_on_period_close", True))
        auth = data.get("auth", {})
        oidc = auth.get("oidc", {})
        s.auth_mode = auth.get("mode", s.auth_mode)
        s.session_hours = float(auth.get("session_hours", s.session_hours))
        s.oidc_issuer = oidc.get("issuer") or None
        s.oidc_client_id = oidc.get("client_id") or None
        s.oidc_client_secret = oidc.get("client_secret") or (
            _env(oidc["client_secret_env"]) if oidc.get("client_secret_env") else None)
        s.oidc_redirect_uri = oidc.get("redirect_uri", s.oidc_redirect_uri)
        s.oidc_frontend_url = oidc.get("frontend_url", s.oidc_frontend_url)
        s.oidc_scopes = tuple(oidc.get("scopes", s.oidc_scopes))
        s.oidc_username_claim = oidc.get("username_claim", s.oidc_username_claim)
        s.oidc_role_claim = oidc.get("role_claim", s.oidc_role_claim)
        s.oidc_role_map = dict(oidc.get("role_map", {}))
        s.oidc_default_role = oidc.get("default_role", s.oidc_default_role) or None

    # environment variables win
    s.backend = _env("RISKMESH_BACKEND") or s.backend
    s.json_path = _env("RISKMESH_JSON_PATH") or s.json_path
    s.sql_url = _env("RISKMESH_SQL_URL") or s.sql_url
    s.neo4j_uri = _env("RISKMESH_NEO4J_URI") or s.neo4j_uri
    s.neo4j_user = _env("RISKMESH_NEO4J_USER") or s.neo4j_user
    s.neo4j_password = _env("RISKMESH_NEO4J_PASSWORD") or s.neo4j_password
    s.neo4j_database = _env("RISKMESH_NEO4J_DATABASE") or s.neo4j_database
    s.api_key = _env("RISKMESH_API_KEY") or s.api_key
    s.auth_mode = _env("RISKMESH_AUTH_MODE") or s.auth_mode
    s.audit_signing_key = _env("RISKMESH_AUDIT_SIGNING_KEY") or s.audit_signing_key
    s.audit_tsa_url = _env("RISKMESH_AUDIT_TSA_URL") or s.audit_tsa_url
    s.oidc_client_secret = _env("RISKMESH_OIDC_CLIENT_SECRET") or s.oidc_client_secret
    if _env("RISKMESH_CORS_ORIGINS"):
        s.cors_origins = [o.strip() for o in _env("RISKMESH_CORS_ORIGINS").split(",") if o.strip()]
    chosen = _env("RISKMESH_WORKSPACE")
    if ws_tables:
        _load_workspaces(s, ws_tables, base)
        if chosen and chosen != s.workspace:
            if chosen not in s.workspaces:
                raise ValueError(f"RISKMESH_WORKSPACE: unknown workspace {chosen!r} "
                                 f"(configured: {', '.join(s.workspaces)})")
            s = s.for_workspace(chosen)
    elif chosen not in (None, "default"):
        raise ValueError(f"RISKMESH_WORKSPACE: unknown workspace {chosen!r} (no [workspaces] configured)")
    return s.validate()


def _load_workspaces(s: Settings, tables: Dict[str, Any], base: Path) -> None:
    """[storage] becomes the workspace "default"; each [workspaces.<id>] table is another one."""
    if "default" in tables:
        raise ValueError("[workspaces.default] is reserved: the default workspace is [storage]")
    s.workspaces = {"default": {"title": s.workspace_title, **{k: getattr(s, k) for k in STORAGE_FIELDS}}}
    sso_global = {"workspace_sso": True, "oidc_role_map": dict(s.oidc_role_map),
                  "oidc_default_role": s.oidc_default_role}
    s.workspaces["default"].update(sso_global)
    known = {"title", "backend", "json_path", "sql_url", "neo4j", "sso", "role_map", "default_role"}
    for wid, t in tables.items():
        unknown = set(t) - known
        if unknown:
            raise ValueError(f"workspaces.{wid}: unknown keys {', '.join(sorted(unknown))}")
        backend = t.get("backend", "json")
        ws: Dict[str, Any] = {**sso_global, "title": t.get("title") or wid, "backend": backend,
                              "json_path": _resolve_path(t.get("json_path", f"riskmesh_{wid}.json"), base),
                              "sql_url": _resolve_sql_url(t["sql_url"], base) if "sql_url" in t else "",
                              "workspace_sso": bool(t.get("sso", True))}
        if backend == "sql" and not ws["sql_url"]:
            raise ValueError(f"workspaces.{wid}: the sql backend needs sql_url")
        neo = t.get("neo4j", {})
        if backend == "neo4j":
            if not neo.get("database"):
                raise ValueError(f"workspaces.{wid}: the neo4j backend needs neo4j.database (one database each)")
            ws.update(neo4j_uri=neo.get("uri", s.neo4j_uri), neo4j_user=neo.get("user", s.neo4j_user),
                      neo4j_database=neo["database"],
                      neo4j_password=neo.get("password") or (
                          _env(neo["password_env"]) if neo.get("password_env") else s.neo4j_password))
        if "role_map" in t:
            ws["oidc_role_map"] = dict(t["role_map"])
        if "default_role" in t:
            ws["oidc_default_role"] = t["default_role"] or None
        s.workspaces[wid] = ws


def write_template(path: Path, overwrite: bool = False) -> Path:
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path
