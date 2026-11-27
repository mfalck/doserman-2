"""
Hashing for the tamper-evident audit chain.

Everything is hashed as canonical JSON (sorted keys, no whitespace) so the
result is identical whichever backend stored the data. Integral floats are
written as integers because some backends return 400000 as 400000.0.
"""

import hashlib
import json
from dataclasses import MISSING, fields
from typing import Any, Dict, Optional

from .models import AuditEntry, Entity

GENESIS = "0" * 64  # prev_hash of the first entry


def _normalise(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return value


def canonical(value: Any) -> str:
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _without_defaults(entity: Entity) -> Dict[str, Any]:
    data = entity.to_dict()
    for f in fields(entity):
        if f.default is not MISSING:
            default = f.default
        elif f.default_factory is not MISSING:
            default = f.default_factory()
        else:
            continue
        if f.name != "id" and canonical(data.get(f.name)) == canonical(default):
            data.pop(f.name, None)
    return data


def state_hash(entity: Optional[Entity]) -> Optional[str]:
    """
    Fingerprint of an entity's stored state. Fields that hold their default
    value are left out, so adding a new field to the model (whose existing
    rows get the default) does not change the fingerprint of existing data.
    """
    return sha256(_without_defaults(entity)) if entity is not None else None


def legacy_state_hash(entity: Optional[Entity]) -> Optional[str]:
    """Fingerprint used before RiskMesh 0.2 (all fields); still accepted by verify."""
    return sha256(entity.to_dict()) if entity is not None else None


# The fields covered by an entry's hash. Frozen: adding a field to AuditEntry
# must not change the hash of existing entries (it would break every chain).
# A future field that needs protecting gets a new list plus a hash version.
HASHED_FIELDS_V1 = ("id", "created_at", "updated_at", "action", "entity_type", "entity_id", "details",
                    "user_id", "timestamp", "seq", "source", "reason", "changes", "state_hash", "prev_hash")


def entry_hash(entry: AuditEntry) -> str:
    """Hash of an audit entry over HASHED_FIELDS_V1 (everything except `hash` itself)."""
    data: Dict[str, Any] = entry.to_dict()
    return sha256({k: data.get(k) for k in HASHED_FIELDS_V1})


IGNORED_IN_DIFF = {"updated_at"}
SECRET_FIELDS = {"password_hash", "token_hash"}  # changes on every save; covered by state_hash


def diff(before: Optional[Entity], after: Optional[Entity]) -> Dict[str, list]:
    """Field-level changes as {field: [before, after]}; one side is None for create/delete."""
    b = before.to_dict() if before is not None else {}
    a = after.to_dict() if after is not None else {}
    out = {}
    for key in sorted(set(a) | set(b)):
        if key in IGNORED_IN_DIFF:
            continue
        old, new = b.get(key), a.get(key)
        if canonical(old) != canonical(new):
            if key in SECRET_FIELDS:  # never copy password/token hashes into the audit log
                old, new = ("***" if old else None), ("***" if new else None)
            out[key] = [old, new]
    return out
