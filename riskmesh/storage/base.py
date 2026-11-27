"""
The storage contract. The business layer only talks to `Repository`;
each backend (JSON, SQL, Neo4j) implements it.

Repositories are deliberately "dumb": they store and fetch entities and
do not enforce business rules (uniqueness, references, cascades).
That is the job of the service layer, so all backends behave the same.

Concurrency: every change made by the service runs inside
`repository.transaction()`. A transaction is atomic (the change and its audit
entry are stored together or not at all) and holds the backend's write lock,
so writers in different threads or processes never interleave and the audit
chain stays linear.
"""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Type, TypeVar

from ..domain.models import Entity

E = TypeVar("E", bound=Entity)


class StorageError(RuntimeError):
    """The backend cannot be used (locked by another process, unreachable, ...)."""


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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic unit of work holding the write lock. Re-entrant. Default: no-op."""
        yield

    def latest(self, cls: Type[E], field: str) -> Optional[E]:
        """The entity with the highest value of `field` (backends may do this without listing all)."""
        items = self.list(cls)
        return max(items, key=lambda e: getattr(e, field)) if items else None

    def close(self) -> None:
        """Releases connections and locks. Optional."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def matches(entity: Entity, filters: dict) -> bool:
    return all(getattr(entity, k) == v for k, v in filters.items())
