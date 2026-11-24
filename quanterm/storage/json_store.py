"""In-memory and JSON-file backends."""

import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

from ..domain.models import ENTITY_TYPES, Entity
from .base import E, Repository, matches

log = logging.getLogger(__name__)
FORMAT_VERSION = "2.0"


class MemoryRepository(Repository):
    """Keeps everything in dictionaries. Useful for tests and demos."""

    def __init__(self):
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {t.kind: {} for t in ENTITY_TYPES}

    def _table(self, cls: Type[Entity]) -> Dict[str, Dict[str, Any]]:
        return self._data[cls.kind]

    def get(self, cls: Type[E], entity_id: str) -> Optional[E]:
        row = self._table(cls).get(entity_id)
        return cls.from_dict(row) if row else None

    def list(self, cls: Type[E], **filters: Any) -> List[E]:
        items = [cls.from_dict(row) for row in self._table(cls).values()]
        return [e for e in items if matches(e, filters)]

    def save(self, entity: Entity) -> None:
        self._table(type(entity))[entity.id] = entity.to_dict()
        self._changed()

    def delete(self, cls: Type[Entity], entity_id: str) -> bool:
        existed = self._table(cls).pop(entity_id, None) is not None
        if existed:
            self._changed()
        return existed

    def clear(self) -> None:
        self._data = {t.kind: {} for t in ENTITY_TYPES}
        self._changed()

    def _changed(self) -> None:
        """Hook for subclasses that persist."""


class JsonRepository(MemoryRepository):
    """
    Stores everything in one JSON file. The file is rewritten atomically
    after each change (fine for small data sets and single users).
    """

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        if os.path.exists(path):
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{self.path} is not valid JSON: {e}") from e
        for t in ENTITY_TYPES:
            self._data[t.kind] = raw.get(t.kind, {})

    def _changed(self) -> None:
        payload = {"version": FORMAT_VERSION, "last_modified": datetime.now().isoformat(), **self._data}
        folder = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
