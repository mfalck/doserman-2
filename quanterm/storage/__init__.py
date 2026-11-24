"""Storage tier. Use `create_repository(settings)` to get the configured backend."""

from .base import Repository
from .json_store import JsonRepository, MemoryRepository


def create_repository(settings) -> Repository:
    backend = settings.backend
    if backend == "json":
        return JsonRepository(settings.json_path)
    if backend == "memory":
        return MemoryRepository()
    if backend == "sql":
        from .sql_store import SqlRepository
        return SqlRepository(settings.sql_url)
    if backend == "neo4j":
        from .neo4j_store import Neo4jRepository
        if not settings.neo4j_password:
            raise RuntimeError("Set QUANTERM_NEO4J_PASSWORD to use the Neo4j backend.")
        return Neo4jRepository(settings.neo4j_uri, settings.neo4j_user,
                               settings.neo4j_password, settings.neo4j_database)
    raise RuntimeError(f"Unknown backend {backend!r} (use json, sql, neo4j or memory)")


__all__ = ["Repository", "MemoryRepository", "JsonRepository", "create_repository"]
