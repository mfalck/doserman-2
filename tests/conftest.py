"""
Shared fixtures. Every test that uses `repo` or `svc` runs once per backend.

Neo4j tests run only when a test database is configured:
  RISKMESH_TEST_NEO4J_URI, RISKMESH_TEST_NEO4J_USER, RISKMESH_TEST_NEO4J_PASSWORD
WARNING: the Neo4j tests delete all RiskMesh nodes in that database.
"""

import os

import pytest

from riskmesh.services import RiskService
from riskmesh.storage import JsonRepository, MemoryRepository

BACKENDS = ["memory", "json", "sql", "neo4j"]


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
