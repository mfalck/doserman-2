"""
Neo4j backend.

Each entity is a node (:BusinessUnit, :Asset, :Person, :Risk). Its fields
are node properties and are the source of truth; dict fields (distribution
specs) are stored as JSON strings because Neo4j properties cannot be maps.

On every save the relationships below are (re)built from those properties,
in both directions, so the graph can be queried directly in Cypher:

  (:BusinessUnit)-[:CHILD_OF]->(:BusinessUnit)
  (:BusinessUnit)-[:HAS_RISK_OWNER]->(:Person)
  (:BusinessUnit)-[:HAS_DELEGATE]->(:Person)
  (:Asset)-[:BELONGS_TO]->(:BusinessUnit)
  (:Risk)-[:BELONGS_TO]->(:BusinessUnit)
  (:Risk)-[:AFFECTS]->(:Asset)
  (:Risk)-[:OWNED_BY]->(:Person)
"""

import json
from dataclasses import fields
from typing import Any, Dict, List, Optional, Type

from ..domain.models import ENTITY_TYPES, Asset, BusinessUnit, Entity, Person, Risk
from .base import E, Repository

LABELS: Dict[Type[Entity], str] = {
    BusinessUnit: "BusinessUnit",
    Asset: "Asset",
    Person: "Person",
    Risk: "Risk",
}

# (from type, property on the from-node, relationship, to type, property is a list)
RELATIONSHIPS = [
    (BusinessUnit, "parent_id", "CHILD_OF", BusinessUnit, False),
    (BusinessUnit, "risk_owner_id", "HAS_RISK_OWNER", Person, False),
    (BusinessUnit, "delegate_ids", "HAS_DELEGATE", Person, True),
    (Asset, "business_unit_id", "BELONGS_TO", BusinessUnit, False),
    (Risk, "business_unit_id", "BELONGS_TO", BusinessUnit, False),
    (Risk, "asset_ids", "AFFECTS", Asset, True),
    (Risk, "owner_id", "OWNED_BY", Person, False),
]


def _dict_fields(cls: Type[Entity]) -> List[str]:
    """Fields typed as Dict -> stored as JSON strings."""
    return [f.name for f in fields(cls) if "Dict" in str(f.type)]


class Neo4jRepository(Repository):

    def __init__(self, uri: str, user: str, password: str, database: Optional[str] = None):
        from neo4j import GraphDatabase  # imported lazily: optional dependency

        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database
        self.driver.verify_connectivity()
        for label in LABELS.values():
            self._run(f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS "
                      f"FOR (n:{label}) REQUIRE n.id IS UNIQUE")

    # -- helpers ----------------------------------------------------------

    def _run(self, query: str, **params: Any):
        records, _, _ = self.driver.execute_query(query, params, database_=self.database)
        return records

    def _to_props(self, entity: Entity) -> Dict[str, Any]:
        props = entity.to_dict()
        for name in _dict_fields(type(entity)):
            if props[name] is not None:
                props[name] = json.dumps(props[name])
        return props

    def _from_props(self, cls: Type[E], props: Dict[str, Any]) -> E:
        data = dict(props)
        for name in _dict_fields(cls):
            if isinstance(data.get(name), str):
                data[name] = json.loads(data[name])
        for f in fields(cls):  # lists may come back missing if they were empty
            if "List" in str(f.type) and data.get(f.name) is None:
                data[f.name] = []
        return cls.from_dict(data)

    def _link(self, entity: Entity) -> None:
        label = LABELS[type(entity)]
        rel_types = [r[2] for r in RELATIONSHIPS if r[0] is type(entity)]
        if rel_types:
            self._run(f"MATCH (n:{label} {{id: $id}})-[r]->() WHERE type(r) IN $types DELETE r",
                      id=entity.id, types=rel_types)
        for src, prop, rel, dst, is_list in RELATIONSHIPS:
            if src is type(entity):  # outgoing: this node references others
                cond = f"t.id IN n.{prop}" if is_list else f"t.id = n.{prop}"
                self._run(f"MATCH (n:{label} {{id: $id}}) MATCH (t:{LABELS[dst]}) WHERE {cond} "
                          f"MERGE (n)-[:{rel}]->(t)", id=entity.id)
            if dst is type(entity):  # incoming: others that reference this node
                cond = f"$id IN s.{prop}" if is_list else f"s.{prop} = $id"
                self._run(f"MATCH (n:{label} {{id: $id}}) MATCH (s:{LABELS[src]}) WHERE {cond} "
                          f"MERGE (s)-[:{rel}]->(n)", id=entity.id)

    # -- Repository API ---------------------------------------------------

    def get(self, cls: Type[E], entity_id: str) -> Optional[E]:
        records = self._run(f"MATCH (n:{LABELS[cls]} {{id: $id}}) RETURN properties(n) AS p", id=entity_id)
        return self._from_props(cls, records[0]["p"]) if records else None

    def list(self, cls: Type[E], **filters: Any) -> List[E]:
        clauses, params = [], {}
        for i, (key, value) in enumerate(filters.items()):
            if key not in cls.field_names():
                raise ValueError(f"Unknown field {key!r} for {cls.__name__}")
            if value is None:
                clauses.append(f"n.{key} IS NULL")
            else:
                clauses.append(f"n.{key} = $v{i}")
                params[f"v{i}"] = value
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        records = self._run(f"MATCH (n:{LABELS[cls]}) {where} RETURN properties(n) AS p", **params)
        return [self._from_props(cls, r["p"]) for r in records]

    def save(self, entity: Entity) -> None:
        label = LABELS[type(entity)]
        self._run(f"MERGE (n:{label} {{id: $id}}) SET n = $props",
                  id=entity.id, props=self._to_props(entity))
        self._link(entity)

    def delete(self, cls: Type[Entity], entity_id: str) -> bool:
        records = self._run(f"MATCH (n:{LABELS[cls]} {{id: $id}}) DETACH DELETE n RETURN count(*) AS c",
                            id=entity_id)
        return bool(records and records[0]["c"])

    def clear(self) -> None:
        labels = " OR ".join(f"n:{LABELS[t]}" for t in ENTITY_TYPES)
        self._run(f"MATCH (n) WHERE {labels} DETACH DELETE n")

    def close(self) -> None:
        self.driver.close()
