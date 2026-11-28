"""In-memory and JSON-file backends."""

import copy
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Type

from ..domain.models import ENTITY_TYPES, Entity
from .base import E, Repository, StorageError, matches

log = logging.getLogger(__name__)
FORMAT_VERSION = "3"


class MemoryRepository(Repository):
    """Keeps everything in dictionaries. Useful for tests and demos."""

    def __init__(self):
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {t.kind: {} for t in ENTITY_TYPES}
        self._lock = threading.RLock()
        self._depth = 0
        self._dirty = False

    def _table(self, cls: Type[Entity]) -> Dict[str, Dict[str, Any]]:
        return self._data[cls.kind]

    def get(self, cls: Type[E], entity_id: str) -> Optional[E]:
        row = self._table(cls).get(entity_id)
        return cls.from_dict(row) if row else None

    def list(self, cls: Type[E], **filters: Any) -> List[E]:
        items = [cls.from_dict(row) for row in list(self._table(cls).values())]
        return [e for e in items if matches(e, filters)]

    def save(self, entity: Entity) -> None:
        with self._lock:
            self._table(type(entity))[entity.id] = entity.to_dict()
            self._changed()

    def delete(self, cls: Type[Entity], entity_id: str) -> bool:
        with self._lock:
            existed = self._table(cls).pop(entity_id, None) is not None
            if existed:
                self._changed()
            return existed

    def clear(self) -> None:
        with self._lock:
            self._data = {t.kind: {} for t in ENTITY_TYPES}
            self._changed()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Thread lock; changes are rolled back on error and written to disk once at the end."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            snapshot = copy.deepcopy(self._data)
            self._depth = 1
            self._dirty = False
            try:
                yield
            except BaseException:
                self._data = snapshot
                self._dirty = False
                raise
            finally:
                self._depth = 0
            if self._dirty:
                self._persist()
                self._dirty = False

    def _changed(self) -> None:
        if self._depth:
            self._dirty = True  # written when the transaction ends
        else:
            self._persist()

    def _persist(self) -> None:
        """Hook for subclasses that persist."""


class _ProcessLock:
    """An exclusive lock on '<file>.lock' for as long as the repository is open."""

    def __init__(self, path: str):
        self.path = path
        self.handle = open(path, "a+b")  # noqa: SIM115 - held open for the lock's lifetime, closed in release()
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.handle.close()
            raise StorageError(
                f"{path[:-5]} is in use by another RiskMesh process (GUI, CLI or API). The JSON backend "
                "supports one process at a time; use the SQL backend for several users.") from e

    def release(self) -> None:
        if self.handle.closed:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


class JsonRepository(MemoryRepository):
    """
    Stores everything in one JSON file, rewritten atomically after each change
    (or once per transaction). Meant for one user: the file is locked by the
    process that opened it, so a second GUI/CLI/API process gets a clear error
    instead of silently overwriting changes.
    """

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self._process_lock = _ProcessLock(os.path.abspath(path) + ".lock")
        if os.path.exists(path):
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            raise StorageError(f"{self.path} is not valid JSON: {e}") from e
        for t in ENTITY_TYPES:
            self._data[t.kind] = raw.get(t.kind, {})

    def _persist(self) -> None:
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

    def close(self) -> None:
        self._process_lock.release()
