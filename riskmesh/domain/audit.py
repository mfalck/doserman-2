"""
Hashing for the tamper-evident audit chain.

Everything is hashed as canonical JSON (sorted keys, no whitespace) so the
result is identical whichever backend stored the data. Integral floats are
written as integers because some backends return 400000 as 400000.0.
"""

import hashlib
import json
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


def state_hash(entity: Optional[Entity]) -> Optional[str]:
    """Fingerprint of an entity's stored state."""
    return sha256(entity.to_dict()) if entity is not None else None


def entry_hash(entry: AuditEntry) -> str:
    """Hash of an audit entry: every field except `hash` itself."""
    data: Dict[str, Any] = entry.to_dict()
    data.pop("hash", None)
    return sha256(data)


IGNORED_IN_DIFF = {"updated_at"}  # changes on every save; covered by state_hash


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
            out[key] = [old, new]
    return out
