"""
Configuration from environment variables (set them in your shell or
service manager; no .env file is read).

  QUANTERM_BACKEND         json | sql | neo4j | memory      (default: json)
  QUANTERM_JSON_PATH       path to the JSON data file        (default: quanterm_data.json)
  QUANTERM_SQL_URL         SQLAlchemy URL                    (default: sqlite:///quanterm.db)
  QUANTERM_NEO4J_URI       e.g. neo4j://localhost:7687
  QUANTERM_NEO4J_USER      default: neo4j
  QUANTERM_NEO4J_PASSWORD  required for the neo4j backend
  QUANTERM_NEO4J_DATABASE  optional database name
  QUANTERM_API_KEY         if set, the API requires header  X-API-Key: <value>
  QUANTERM_CORS_ORIGINS    comma-separated origins allowed to call the API
                           (e.g. https://doserman.lovable.app)
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    backend: str = field(default_factory=lambda: _env("QUANTERM_BACKEND", "json"))
    json_path: str = field(default_factory=lambda: _env("QUANTERM_JSON_PATH", "quanterm_data.json"))
    sql_url: str = field(default_factory=lambda: _env("QUANTERM_SQL_URL", "sqlite:///quanterm.db"))
    neo4j_uri: str = field(default_factory=lambda: _env("QUANTERM_NEO4J_URI", "neo4j://localhost:7687"))
    neo4j_user: str = field(default_factory=lambda: _env("QUANTERM_NEO4J_USER", "neo4j"))
    neo4j_password: Optional[str] = field(default_factory=lambda: _env("QUANTERM_NEO4J_PASSWORD"))
    neo4j_database: Optional[str] = field(default_factory=lambda: _env("QUANTERM_NEO4J_DATABASE"))
    api_key: Optional[str] = field(default_factory=lambda: _env("QUANTERM_API_KEY"))
    cors_origins: List[str] = field(default_factory=lambda: [
        o.strip() for o in (_env("QUANTERM_CORS_ORIGINS", "") or "").split(",") if o.strip()])
