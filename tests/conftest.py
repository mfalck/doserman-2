"""
Shared fixtures. Every test that uses `repo` or `svc` runs once per backend.

PostgreSQL and Neo4j tests run only when a test database is configured
(the CI workflow starts both as services):
  RISKMESH_TEST_POSTGRES_URL   e.g. postgresql+psycopg://riskmesh:pw@localhost:5432/riskmesh_test
  RISKMESH_TEST_NEO4J_URI, RISKMESH_TEST_NEO4J_USER, RISKMESH_TEST_NEO4J_PASSWORD
WARNING: these tests delete all RiskMesh data in those databases.
"""

import os

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
