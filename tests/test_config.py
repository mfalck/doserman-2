"""riskmesh.toml loading, precedence and the service it produces."""

import pytest

from riskmesh.bootstrap import make_service
from riskmesh.config import TEMPLATE, load_settings, write_template


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("RISKMESH_CONFIG", "RISKMESH_BACKEND", "RISKMESH_JSON_PATH", "RISKMESH_SQL_URL",
                 "RISKMESH_NEO4J_PASSWORD", "RISKMESH_API_KEY", "RISKMESH_CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no riskmesh.toml in the working folder
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def write(tmp_path, text):
    p = tmp_path / "cfg" / "riskmesh.toml"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_without_a_file():
    s = load_settings()
    assert s.config_file is None and s.backend == "json" and s.engine == "analytic"


def test_template_is_valid_and_matches_defaults(tmp_path):
    p = write_template(tmp_path / "riskmesh.toml")
    s = load_settings(str(p))
    assert s.config_file == str(p) and s.backend == "json"
    assert s.likelihood_bands == (0.05, 0.20, 0.50, 0.80)
    with pytest.raises(FileExistsError):
        write_template(p)
    assert "[storage]" in TEMPLATE


def test_relative_paths_are_relative_to_the_file(tmp_path):
    p = write(tmp_path, '[storage]\nbackend = "sql"\njson_path = "data/x.json"\nsql_url = "sqlite:///db/r.db"\n')
    s = load_settings(str(p))
    assert s.json_path.replace("\\", "/").endswith("cfg/data/x.json")
    assert s.sql_url.startswith("sqlite:///") and s.sql_url.endswith("cfg/db/r.db")


def test_found_in_working_folder_and_env_variable(tmp_path, monkeypatch):
    (tmp_path / "riskmesh.toml").write_text('[storage]\nbackend = "memory"\n', encoding="utf-8")
    assert load_settings().backend == "memory"
    other = write(tmp_path, '[storage]\nbackend = "sql"\n')
    monkeypatch.setenv("RISKMESH_CONFIG", str(other))
    assert load_settings().backend == "sql"


def test_environment_overrides_file_and_secrets_come_from_env(tmp_path, monkeypatch):
    p = write(tmp_path, '[storage]\nbackend = "sql"\n[storage.neo4j]\npassword_env = "MY_NEO4J_PW"\n'
                        '[api]\napi_key_env = "MY_KEY"\ncors_origins = ["http://a"]\n')
    monkeypatch.setenv("RISKMESH_BACKEND", "memory")
    monkeypatch.setenv("MY_NEO4J_PW", "secret")
    monkeypatch.setenv("MY_KEY", "k")
    s = load_settings(str(p))
    assert s.backend == "memory" and s.neo4j_password == "secret" and s.api_key == "k"
    assert s.cors_origins == ["http://a"]
    assert s.public()["neo4j_password"] == "***" and s.public()["api_key"] == "***"


@pytest.mark.parametrize("text, message", [
    ('[storage]\nbackend = "excel"\n', "backend"),
    ('[analysis]\nengine = "guess"\n', "engine"),
    ('[scoring]\nimpact_bands = [3, 2, 1, 0]\n', "impact_bands"),
    ('[storage\nbackend = "json"\n', ""),
])
def test_invalid_files_are_rejected(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_settings(str(write(tmp_path, text)))


def test_missing_explicit_file():
    with pytest.raises(FileNotFoundError):
        load_settings("does/not/exist.toml")


def test_service_uses_scoring_and_user_from_config(tmp_path):
    p = write(tmp_path, '[storage]\nbackend = "memory"\n[scoring]\nimpact_bands = [1, 2, 3, 4]\n'
                        '[user]\nname = "Søren"\n')
    svc = make_service(load_settings(str(p)), source="gui")
    assert svc.actor == "Søren" and svc.source == "gui"
    bu = svc.create_business_unit("A")
    r = svc.create_risk(bu.id, "r", {"type": "poisson", "rate": 1}, {"dist": "fixed", "value": 10})
    assert svc.risk_scores(r)["inherent"]["impact"] == 5  # 10 >= 4 with these bands
