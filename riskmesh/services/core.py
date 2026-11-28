"""
The core every service area builds on: the repository, the audit chain,
generic create / update / delete with reference checks and cascades, and the
small validators. Areas (organisation and risks, third parties, portfolio ...)
inherit from it today and will receive it as a collaborator once they are
split out of RiskService; either way this is the only place that writes.
"""

import copy
import typing
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Set, Tuple, Type

from ..domain import audit, risk_model
from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import REFERENCES, AuditEntry, Entity
from ..storage.base import E, Repository


@dataclass
class ScoringScale:
    """Thresholds that turn quantitative specs into 1-5 scores."""
    likelihood_bands: tuple = risk_model.DEFAULT_LIKELIHOOD_BANDS  # P(>=1 event / year)
    impact_bands: tuple = risk_model.DEFAULT_IMPACT_BANDS          # median loss per event


def _package_version() -> str:
    from .. import __version__
    return __version__


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_optional(cls: Type[Entity], field_name: str) -> bool:
    return type(None) in typing.get_args(typing.get_type_hints(cls)[field_name])


class ServiceCore:
    """Repository, audit chain and generic persistence. Subclasses provide _validate."""

    def __init__(self, repo: Repository, scoring: Optional[ScoringScale] = None, actor: str = "system",
                 source: str = "system"):
        self.repo = repo
        self.scoring = scoring or ScoringScale()
        # Context recorded in the audit log; set per request by the API / per command by the CLI.
        self.actor = actor
        self.source = source
        self.reason: Optional[str] = None
        self._last_audit_ts: Optional[datetime] = None
        self._head: Optional[Tuple[int, str]] = None  # (seq, hash) of the latest audit entry
        self._tx_depth = 0

    @contextmanager
    def context(self, actor: Optional[str] = None, source: Optional[str] = None, reason: Optional[str] = None):
        """Temporarily set who/where/why for the audit log."""
        saved = (self.actor, self.source, self.reason)
        self.actor = actor or self.actor
        self.source = source or self.source
        self.reason = reason if reason is not None else self.reason
        try:
            yield self
        finally:
            self.actor, self.source, self.reason = saved

    # =====================================================================
    # Generic machinery
    # =====================================================================

    def _require(self, cls: Type[E], entity_id: Optional[str], what: Optional[str] = None) -> E:
        label = what or cls.__name__
        if not entity_id:
            raise ValidationError(f"{label} ID cannot be empty.")
        entity = self.repo.get(cls, entity_id)
        if entity is None:
            raise NotFoundError(f"{label} '{entity_id}' not found.")
        return entity

    def _chain_head(self) -> Tuple[int, str]:
        if self._head is None:
            last = self.repo.latest(AuditEntry, "seq")
            self._head = (last.seq, last.hash) if last else (0, audit.GENESIS)
        return self._head

    def _audit(self, action: str, entity_type: str, entity_id: Optional[str], details: Optional[str] = None,
               changes: Optional[Dict[str, Any]] = None, state: Optional[str] = None) -> AuditEntry:
        """Appends one entry to the hash chain."""
        # Microsecond timestamps, forced strictly increasing so the log order is exact.
        ts = datetime.now(timezone.utc)
        if self._last_audit_ts and ts <= self._last_audit_ts:
            ts = self._last_audit_ts + timedelta(microseconds=1)
        self._last_audit_ts = ts
        seq, prev = self._chain_head()
        entry = AuditEntry(action=action, entity_type=entity_type, entity_id=entity_id, details=details,
                           user_id=self.actor, timestamp=ts.isoformat(timespec="microseconds"),
                           seq=seq + 1, source=self.source, reason=self.reason, changes=changes,
                           state_hash=state, prev_hash=prev)
        entry.hash = audit.entry_hash(entry)
        self.repo.save(entry)
        self._head = (entry.seq, entry.hash)
        return entry

    def _save(self, entity: Entity, action: str, before: Optional[Entity] = None,
              details: Optional[str] = None, stamp: bool = True) -> None:
        if stamp:
            now = _now()
            entity.created_at = entity.created_at or now
            entity.updated_at = now
        self.repo.save(entity)
        # hash what the backend actually stored, so verify() compares like with like
        stored = self.repo.get(type(entity), entity.id) or entity
        self._audit(action, entity.kind, entity.id, details or getattr(entity, "name", None),
                    changes=audit.diff(before, stored), state=audit.state_hash(stored))

    def _remove(self, entity: Entity, details: Optional[str] = None) -> None:
        self.repo.delete(type(entity), entity.id)
        self._audit("DELETE", entity.kind, entity.id, details or getattr(entity, "name", None),
                    changes=audit.diff(entity, None), state=None)

    def _create(self, cls: Type[E], **data: Any) -> E:
        try:
            entity = cls(**data)
        except TypeError as e:
            raise ValidationError(str(e)) from e
        self._validate(entity)
        self._check_refs(entity)
        self._save(entity, "CREATE")
        return entity

    def _update(self, cls: Type[E], entity_id: str, changes: Dict[str, Any]) -> E:
        entity = self._require(cls, entity_id)
        before = copy.deepcopy(entity)
        unknown = set(changes) - (set(cls.field_names()) - {"id", "created_at", "updated_at"})
        if unknown:
            raise ValidationError(f"Unknown fields for {cls.__name__}: {', '.join(sorted(unknown))}")
        for key, value in changes.items():
            setattr(entity, key, value)
        self._validate(entity)
        self._check_refs(entity)
        self._save(entity, "UPDATE", before)
        return entity

    def _delete(self, cls: Type[Entity], entity_id: str) -> None:
        """
        Deletes an entity and keeps the data consistent:
        required references to it -> the referring entities are deleted too;
        optional references -> set to None; list references -> ID removed.
        """
        entity = self._require(cls, entity_id)
        for src, field_name, dst, is_list, _ in REFERENCES:
            if dst is not cls:
                continue
            for ref in self.repo.list(src):
                value = getattr(ref, field_name)
                before = copy.deepcopy(ref)
                if is_list and entity_id in value:
                    setattr(ref, field_name, [v for v in value if v != entity_id])
                    self._save(ref, "UPDATE", before, f"removed reference to deleted {cls.kind} {entity_id}")
                elif not is_list and value == entity_id:
                    if _is_optional(src, field_name):
                        setattr(ref, field_name, None)
                        self._save(ref, "UPDATE", before, f"cleared {field_name} (deleted {cls.kind})")
                    else:
                        self._delete(src, ref.id)
        self._remove(entity)

    def _check_refs(self, entity: Entity, known_ids: Optional[Dict[type, Set[str]]] = None) -> None:
        """Every referenced ID must exist (in the repo, or in known_ids during import)."""
        for src, field_name, dst, is_list, _ in REFERENCES:
            if src is not type(entity):
                continue
            value = getattr(entity, field_name)
            for ref_id in (value or []) if is_list else ([value] if value else []):
                exists = ref_id in known_ids[dst] if known_ids is not None else self.repo.get(dst, ref_id)
                if not exists:
                    raise NotFoundError(f"{type(entity).__name__}.{field_name}: {dst.__name__} '{ref_id}' not found.")

    # -- field validation (no repository access, so it also works on import batches) --

    @staticmethod
    def _name(entity: Entity, what: str) -> None:
        if not entity.name or not str(entity.name).strip():
            raise ValidationError(f"{what} name cannot be empty.")
        entity.name = str(entity.name).strip()

    @staticmethod
    def _enum(value: Any, allowed: Iterable[str], field_name: str) -> None:
        if value not in allowed:
            raise ValidationError(f"{field_name} must be one of {', '.join(allowed)} (got {value!r}).")

    @staticmethod
    def _date(value: Any, field_name: str) -> None:
        try:
            date.fromisoformat(str(value)[:10])
        except ValueError as e:
            raise ValidationError(f"{field_name} must be an ISO date (YYYY-MM-DD), got {value!r}.") from e

    @staticmethod
    def _non_negative(value: Any, field_name: str) -> None:
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            raise ValidationError(f"{field_name} must be a non-negative number.")

    def _validate(self, e: Entity) -> None:  # provided by the service that knows the entity types
        raise NotImplementedError

