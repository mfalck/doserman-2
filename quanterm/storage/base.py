"""
The storage contract. The business layer only talks to `Repository`;
each backend (JSON, SQL, Neo4j) implements it.

Repositories are deliberately "dumb": they store and fetch entities and
do not enforce business rules (uniqueness, references, cascades).
That is the job of the service layer, so all backends behave the same.
"""

from abc import ABC, abstractmethod
from typing import Any, List, Optional, Type, TypeVar

from ..domain.models import Entity

E = TypeVar("E", bound=Entity)


class Repository(ABC):

    @abstractmethod
    def get(self, cls: Type[E], entity_id: str) -> Optional[E]:
        """Returns the entity or None."""

    @abstractmethod
    def list(self, cls: Type[E], **filters: Any) -> List[E]:
        """
        Returns all entities of a type. Filters are exact matches on
        scalar fields, e.g. list(Asset, business_unit_id="...").
        """

    @abstractmethod
    def save(self, entity: Entity) -> None:
        """Inserts or replaces the entity (upsert by id)."""

    @abstractmethod
    def delete(self, cls: Type[Entity], entity_id: str) -> bool:
        """Deletes the entity. Returns False if it did not exist."""

    @abstractmethod
    def clear(self) -> None:
        """Deletes everything."""

    def close(self) -> None:
        """Releases connections. Optional."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def matches(entity: Entity, filters: dict) -> bool:
    return all(getattr(entity, k) == v for k, v in filters.items())
