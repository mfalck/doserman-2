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
  RISKMESH_API_KEY, RISKMESH_CORS_ORIGINS (comma-separated)

`riskmesh config init` writes a commented template; `riskmesh config show`
prints the effective settings.
"""

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

FILE_NAME = "riskmesh.toml"
BACKENDS = ("json", "sql", "neo4j", "memory")
ENGINES = ("analytic", "monte_carlo")

TEMPLATE = '''# RiskMesh configuration. Environment variables (RISKMESH_*) override these values.

[storage]
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

[user]
name = ""                           # name recorded in the audit log by the GUI/CLI (default: login name)
'''


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
    user_name: Optional[str] = None
    config_file: Optional[str] = None  # which file was loaded, if any

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
        return self

    def public(self) -> Dict[str, Any]:
        """Settings for display, with secrets masked."""
        d = asdict(self)
        for secret in ("neo4j_password", "api_key"):
            d[secret] = "***" if d[secret] else None
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
        s.user_name = data.get("user", {}).get("name") or None

    # environment variables win
    s.backend = _env("RISKMESH_BACKEND") or s.backend
    s.json_path = _env("RISKMESH_JSON_PATH") or s.json_path
    s.sql_url = _env("RISKMESH_SQL_URL") or s.sql_url
    s.neo4j_uri = _env("RISKMESH_NEO4J_URI") or s.neo4j_uri
    s.neo4j_user = _env("RISKMESH_NEO4J_USER") or s.neo4j_user
    s.neo4j_password = _env("RISKMESH_NEO4J_PASSWORD") or s.neo4j_password
    s.neo4j_database = _env("RISKMESH_NEO4J_DATABASE") or s.neo4j_database
    s.api_key = _env("RISKMESH_API_KEY") or s.api_key
    if _env("RISKMESH_CORS_ORIGINS"):
        s.cors_origins = [o.strip() for o in _env("RISKMESH_CORS_ORIGINS").split(",") if o.strip()]
    return s.validate()


def write_template(path: Path, overwrite: bool = False) -> Path:
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path
