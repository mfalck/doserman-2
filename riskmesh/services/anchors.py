"""Anchoring the audit chain (mixed into RiskService). See domain/anchoring.py for the why."""

import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..domain import anchoring
from ..domain.errors import ValidationError
from ..domain.models import AuditAnchor, AuditEntry


class AnchorAdmin:
    anchor_key_path: Optional[str] = None   # set from [audit] signing_key
    tsa_url: Optional[str] = None           # set from [audit] tsa_url
    anchor_on_period_close: bool = True

    def anchoring_enabled(self) -> bool:
        return bool(self.anchor_key_path or self.tsa_url)

    def create_anchor(self, reason: Optional[str] = None) -> AuditAnchor:
        """Signs and/or time-stamps the current chain head and stores the anchor."""
        if not self.anchoring_enabled():
            raise ValidationError("Anchoring is not configured: set [audit] signing_key and/or tsa_url.")
        head = self.repo.latest(AuditEntry, "seq")
        if head is None:
            raise ValidationError("The audit log is empty; there is nothing to anchor yet.")
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = anchoring.statement(head.seq, head.hash, created)
        anchor = AuditAnchor(seq=head.seq, head_hash=head.hash, statement=text, reason=reason,
                             created_by=self.actor)
        anchor.created_at = anchor.updated_at = created
        if self.anchor_key_path:
            key = anchoring.load_private_key(self.anchor_key_path)
            anchor.public_key = anchoring.public_pem(key)
            anchor.key_id = anchoring.key_id(anchor.public_key)
            anchor.signature = anchoring.sign(key, text)
        if self.tsa_url:  # network call outside the write lock
            token, when = anchoring.request_timestamp(self.tsa_url, bytes.fromhex(head.hash))
            anchor.tsa_url, anchor.tsa_token, anchor.tsa_time = self.tsa_url, base64.b64encode(token).decode(), when
        self._store_anchor(anchor)
        return anchor

    def _store_anchor(self, anchor: AuditAnchor) -> None:
        self.repo.save(anchor)
        self._audit("ANCHOR", anchor.kind, anchor.id, anchoring.describe(anchor.to_dict()),
                    changes={"anchored_seq": [None, anchor.seq], "anchored_hash": [None, anchor.head_hash]})

    def list_anchors(self) -> List[AuditAnchor]:
        return sorted(self.repo.list(AuditAnchor), key=lambda a: (a.seq, a.created_at or ""), reverse=True)

    def check_anchors(self, entries_by_seq: Dict[int, AuditEntry]) -> Dict[str, Any]:
        """Used by verify_audit(): every anchor must still match the chain and its proofs must hold."""
        errors, checked = [], []
        for a in sorted(self.repo.list(AuditAnchor), key=lambda a: a.seq):
            problems = []
            entry = entries_by_seq.get(a.seq)
            if entry is None:
                problems.append(f"entry #{a.seq} was anchored but is gone: the newest entries were removed")
            elif entry.hash != a.head_hash:
                problems.append(f"entry #{a.seq} differs from what was anchored")
            if a.statement != anchoring.statement(a.seq, a.head_hash, a.created_at or ""):
                problems.append("the anchor's statement does not match its fields")
            if a.signature and not (a.public_key and anchoring.verify_signature(a.public_key, a.statement, a.signature)):
                problems.append("the signature is not valid")
            if a.tsa_token:
                try:
                    anchoring.parse_timestamp_token(base64.b64decode(a.tsa_token), bytes.fromhex(a.head_hash))
                except ValidationError as e:
                    problems.append(f"time stamp: {e}")
            for p in problems:
                errors.append({"seq": a.seq, "anchor_id": a.id, "error": p})
            checked.append({"id": a.id, "seq": a.seq, "key_id": a.key_id, "tsa_time": a.tsa_time,
                            "reason": a.reason, "ok": not problems})
        return {"anchors": checked, "anchor_errors": errors}


ANCHOR_WRITE_METHODS = ("_store_anchor",)
