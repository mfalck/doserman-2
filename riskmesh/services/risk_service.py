"""
Business tier. All rules live here: validation, references between
entities, cascades on delete, audit trail, derived scores, import/export
and the quantitative model.

It depends only on the `Repository` interface, so it works unchanged with
the JSON, SQL and Neo4j backends, and it knows nothing about HTTP or GUIs.
"""

import copy
import functools
import logging
import typing
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Type

from ..domain import analytic, audit, risk_model
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..domain.models import (ENTITY_TYPES, AnalysisRun, CONTROL_EVENT_STATUSES, CONTROL_EVENT_TYPES, CONTROL_TYPES, DATA_TYPES,
                             VERIFIED_TYPES, ApiToken, User,
                             IMPLEMENTATION_STATUSES, ORGANIZATION_TYPES, REFERENCES, RISK_EVENT_STATUSES,
                             RISK_EVENT_TYPES, Asset, AuditEntry, BusinessUnit, Control, ControlEvent, Entity,
                             Person, Risk, RiskEvent)
from ..storage.base import E, Repository
from .anchors import ANCHOR_WRITE_METHODS, AnchorAdmin
from .dora import DoraExtract
from .excel import ExcelExchange
from .incidents import INCIDENT_WRITE_METHODS, Incidents
from .portfolio import PORTFOLIO_WRITE_METHODS, Portfolio
from .reporting import REPORTING_WRITE_METHODS, Reporting
from .third_party import THIRD_PARTY_WRITE_METHODS, ThirdParty
from .users import USER_WRITE_METHODS, UserAdmin

log = logging.getLogger(__name__)

EXPORT_VERSION = "3.0"
CONTROL_TEST_SCORE = {"PASSED": 1.0, "PARTIAL": 0.5, "FAILED": 0.0}


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


class RiskService(UserAdmin, AnchorAdmin, Reporting, ThirdParty, Portfolio, Incidents, DoraExtract, ExcelExchange):

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

    def reset_all(self) -> None:
        """
        Deletes all risk data, runs and the audit log (demo/testing); the chain
        starts again. Users and their tokens are kept (otherwise nobody could log
        in afterwards) and are recorded at the start of the new chain.
        """
        users, tokens = self.repo.list(User), self.repo.list(ApiToken)
        self.repo.clear()
        self._head = None
        self._last_audit_ts = None
        for t in tokens:
            self.repo.save(t)
        for u in users:
            self._save(u, "CREATE", details=f"{u.username} (kept when the data was reset)", stamp=False)

    # =====================================================================
    # Backup and restore (everything, including the audit trail)
    # =====================================================================

    def backup_data(self) -> Dict[str, Any]:
        """
        A complete copy: risk data, audit trail, runs, users and token hashes.
        Unlike export_data() it can rebuild a system with its history intact,
        also on a different backend. Contains password and token hashes: keep
        backups as confidential as the database itself.
        """
        content = {cls.kind: [e.to_dict() for e in self.repo.list(cls)] for cls in ENTITY_TYPES}
        head = self.repo.latest(AuditEntry, "seq")
        return {
            "format": "riskmesh-backup",
            "format_version": 1,
            "package_version": _package_version(),
            "created_at": _now(),
            "audit_head_seq": head.seq if head else 0,
            "audit_head_hash": head.hash if head else audit.GENESIS,
            "content_sha256": audit.sha256(content),
            "content": content,
        }

    def restore_data(self, backup: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """
        Restores a backup into this (empty) storage exactly as it was, then
        verifies the audit chain. With force=True existing data is deleted first.
        """
        if backup.get("format") != "riskmesh-backup":
            raise ValidationError("Not a RiskMesh backup file (use import for exports).")
        content = backup.get("content") or {}
        if audit.sha256(content) != backup.get("content_sha256"):
            raise ValidationError("The backup file is damaged or was modified (checksum mismatch).")
        with self.repo.transaction():
            if any(self.repo.list(cls) for cls in ENTITY_TYPES):
                if not force:
                    raise ConflictError("The target storage is not empty; restore with force=True to replace it.")
                self.repo.clear()
            counts = {}
            for cls in ENTITY_TYPES:
                rows = content.get(cls.kind, [])
                for row in rows:
                    self.repo.save(cls.from_dict(row))
                counts[cls.kind] = len(rows)
            self._head = None
            self._last_audit_ts = None
        result = self.verify_audit()
        if result["head_hash"] != backup.get("audit_head_hash"):
            result["ok"] = False
            result["chain_errors"].append({"seq": result["head_seq"],
                                           "error": "restored chain head differs from the backup's head"})
        return {"restored": counts, "verification": result}

    # =====================================================================
    # Audit verification
    # =====================================================================

    def verify_audit(self, check_data: bool = True) -> Dict[str, Any]:
        """
        Checks that the audit chain is intact and (optionally) that the stored
        data matches the last audited state of every entity.

        Detects: altered entries, removed or inserted entries, reordering, and
        data changed or deleted outside RiskMesh. It cannot detect removal of
        the newest entries unless the returned head hash is kept somewhere else
        and compared later ("anchoring").
        """
        entries = sorted(self.repo.list(AuditEntry), key=lambda e: e.seq)
        chain_errors: List[Dict[str, Any]] = []
        prev = audit.GENESIS
        for expected_seq, e in enumerate(entries, start=1):
            if e.seq != expected_seq:
                chain_errors.append({"seq": e.seq,
                                     "error": f"expected entry {expected_seq}: entries missing or inserted"})
            if e.prev_hash != prev:
                chain_errors.append({"seq": e.seq, "error": "prev_hash does not match the previous entry"})
            if audit.entry_hash(e) != e.hash:
                chain_errors.append({"seq": e.seq, "error": "entry content was altered (hash mismatch)"})
            prev = e.hash

        data_errors: List[Dict[str, Any]] = []
        if check_data:
            latest: Dict[Tuple[str, str], AuditEntry] = {}
            for e in entries:
                if e.entity_id and e.action in ("CREATE", "UPDATE", "DELETE"):
                    latest[(e.entity_type, e.entity_id)] = e
            seen = set()
            for cls in VERIFIED_TYPES:
                for entity in self.repo.list(cls):
                    key = (cls.kind, entity.id)
                    seen.add(key)
                    last = latest.get(key)
                    if last is None:
                        error = "not in the audit log"
                    elif last.action == "DELETE":
                        error = "deleted according to the audit log, but still present"
                    elif last.state_hash not in (audit.state_hash(entity), audit.legacy_state_hash(entity)):
                        error = "changed outside RiskMesh (differs from the last audited state)"
                    else:
                        continue
                    data_errors.append({"entity_type": cls.kind, "entity_id": entity.id, "error": error})
            for key, last in latest.items():
                if key not in seen and last.action != "DELETE":
                    data_errors.append({"entity_type": key[0], "entity_id": key[1],
                                        "error": "missing from the data, but not deleted in the audit log"})

        anchors = self.check_anchors({e.seq: e for e in entries})
        if check_data:
            data_errors.extend(self.check_periods())
        return {
            "ok": not chain_errors and not data_errors and not anchors["anchor_errors"],
            **anchors,
            "entries": len(entries),
            "head_seq": entries[-1].seq if entries else 0,
            "head_hash": entries[-1].hash if entries else audit.GENESIS,
            "chain_errors": chain_errors,
            "data_errors": data_errors,
            "checked_data": check_data,
            "verified_at": _now(),
        }

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

    def _validate(self, e: Entity) -> None:
        if isinstance(e, BusinessUnit):
            self._name(e, "Business unit")
            self._enum(e.organization_type, ORGANIZATION_TYPES, "organization_type")
            e.delegate_ids = list(dict.fromkeys(e.delegate_ids or []))
            self._lei(e)
        elif isinstance(e, Asset):
            self._name(e, "Asset")
            if not e.asset_type:
                raise ValidationError("Asset type cannot be empty.")
            self._non_negative(e.value, "value")
        elif isinstance(e, Person):
            self._name(e, "Person")
        elif isinstance(e, Risk):
            self._name(e, "Risk")
            e.asset_ids = list(dict.fromkeys(e.asset_ids or []))
            e.service_ids = list(dict.fromkeys(e.service_ids or []))
            e.supplier_ids = list(dict.fromkeys(e.supplier_ids or []))
            try:
                risk_model.validate_frequency(e.frequency)
                risk_model.build_impact_distribution(e.impact)
                if e.residual_frequency:
                    risk_model.validate_frequency(e.residual_frequency)
                if e.residual_impact:
                    risk_model.build_impact_distribution(e.residual_impact)
                for label, spec in (("outage_hours", e.outage_hours),
                                    ("residual_outage_hours", e.residual_outage_hours)):
                    if spec:
                        dist = risk_model.build_impact_distribution(spec)
                        low = float(spec["value"]) if dist is None else float(dist.ppf(0.001))
                        if low < 0:
                            raise ValueError(f"{label} cannot be negative")
            except (ValueError, TypeError, AttributeError) as ex:
                raise ValidationError(f"Risk '{e.name}': {ex}") from ex
        elif isinstance(e, Control):
            self._name(e, "Control")
            self._enum(e.control_type, CONTROL_TYPES, "control_type")
            self._enum(e.implementation_status, IMPLEMENTATION_STATUSES, "implementation_status")
            if e.effectiveness_rating is not None and e.effectiveness_rating not in (1, 2, 3, 4, 5):
                raise ValidationError("effectiveness_rating must be 1-5.")
            e.risk_ids = list(dict.fromkeys(e.risk_ids or []))
        elif isinstance(e, RiskEvent):
            self._name(e, "Risk event")
            self._enum(e.event_type, RISK_EVENT_TYPES, "event_type")
            self._enum(e.status, RISK_EVENT_STATUSES, "status")
            self._date(e.date, "date")
            self._non_negative(e.financial_impact, "financial_impact")
        elif isinstance(e, ControlEvent):
            self._enum(e.event_type, CONTROL_EVENT_TYPES, "event_type")
            self._enum(e.status, CONTROL_EVENT_STATUSES, "status")
            self._date(e.date, "date")
        elif not self._validate_portfolio(e) and not self._validate_incident(e):
            self._validate_third_party(e)
        # Unit name uniqueness and hierarchy cycles need the repository:
        # see _check_unit_rules and import_data.

    # =====================================================================
    # Business units
    # =====================================================================

    def _descendant_ids(self, unit_id: str, units: Optional[List[BusinessUnit]] = None) -> List[str]:
        """unit_id and all units below it."""
        children: Dict[Optional[str], List[str]] = defaultdict(list)
        for bu in units if units is not None else self.repo.list(BusinessUnit):
            children[bu.parent_id].append(bu.id)
        out, stack = [], [unit_id]
        while stack:
            current = stack.pop()
            if current not in out:
                out.append(current)
                stack.extend(children.get(current, []))
        return out

    def _check_unit_rules(self, bu: BusinessUnit) -> None:
        for other in self.repo.list(BusinessUnit, name=bu.name.strip() if bu.name else bu.name):
            if other.id != bu.id:
                raise ConflictError(f"Business unit with name '{bu.name}' already exists.")
        if bu.parent_id and bu.parent_id in self._descendant_ids(bu.id):
            raise ValidationError("A business unit cannot be placed below itself.")

    def create_business_unit(self, name: str, description: Optional[str] = None,
                             parent_id: Optional[str] = None, **optional: Any) -> BusinessUnit:
        bu = BusinessUnit(name=name, description=description, parent_id=parent_id, **optional)
        self._validate(bu)
        self._check_unit_rules(bu)
        return self._create(BusinessUnit, **{k: v for k, v in bu.to_dict().items()
                                             if k not in ("created_at", "updated_at")})

    def get_business_unit(self, unit_id: str) -> BusinessUnit:
        return self._require(BusinessUnit, unit_id, "Business unit")

    def list_business_units(self) -> List[BusinessUnit]:
        return sorted(self.repo.list(BusinessUnit), key=lambda b: b.name)

    def update_business_unit(self, unit_id: str, **changes: Any) -> BusinessUnit:
        candidate = self.get_business_unit(unit_id)
        for k, v in changes.items():
            if hasattr(candidate, k):
                setattr(candidate, k, v)
        if "name" in changes:
            self._validate(candidate)
        self._lei(candidate)
        self._check_unit_rules(candidate)
        return self._update(BusinessUnit, unit_id, changes)

    def delete_business_unit(self, unit_id: str, cascade: bool = False) -> List[str]:
        """
        Deletes a unit with everything that belongs to it (assets, risks,
        controls, events). If it has child units, `cascade=True` is required
        and the whole subtree is deleted. Returns the deleted unit IDs.
        """
        self.get_business_unit(unit_id)
        units = self._descendant_ids(unit_id)
        if len(units) > 1 and not cascade:
            raise ConflictError("Business unit has child units; delete with cascade=True.")
        for uid in reversed(units):  # children first
            self._delete_unit_appetites(uid)
            self._delete(BusinessUnit, uid)
        return units

    def assign_risk_owner(self, unit_id: str, person_id: Optional[str]) -> BusinessUnit:
        """Sets (or with person_id=None clears) the risk owner."""
        return self.update_business_unit(unit_id, risk_owner_id=person_id)

    def add_delegate(self, unit_id: str, person_id: str) -> BusinessUnit:
        bu = self.get_business_unit(unit_id)
        self._require(Person, person_id, "Person")
        if person_id in bu.delegate_ids:
            return bu
        return self._update(BusinessUnit, unit_id, {"delegate_ids": bu.delegate_ids + [person_id]})

    def remove_delegate(self, unit_id: str, person_id: str) -> BusinessUnit:
        bu = self.get_business_unit(unit_id)
        if person_id not in bu.delegate_ids:
            return bu
        return self._update(BusinessUnit, unit_id, {"delegate_ids": [d for d in bu.delegate_ids if d != person_id]})

    def organisation_tree(self) -> List[Dict[str, Any]]:
        """Nested hierarchy: [{id, name, ..., children: [...]}, ...]."""
        units = self.repo.list(BusinessUnit)
        known = {u.id for u in units}
        nodes = {u.id: {**u.to_dict(), "children": []} for u in units}
        roots = []
        for u in sorted(units, key=lambda x: x.name):
            if u.parent_id in known:
                nodes[u.parent_id]["children"].append(nodes[u.id])
            else:
                roots.append(nodes[u.id])
        return roots

    # =====================================================================
    # Assets and persons
    # =====================================================================

    def create_asset(self, unit_id: str, name: str, asset_type: str, description: Optional[str] = None,
                     criticality: Optional[str] = None, **optional: Any) -> Asset:
        self.get_business_unit(unit_id)
        return self._create(Asset, name=name, asset_type=asset_type, business_unit_id=unit_id,
                            description=description, criticality=criticality, **optional)

    def get_asset(self, asset_id: str) -> Asset:
        return self._require(Asset, asset_id, "Asset")

    def list_assets(self, unit_id: Optional[str] = None) -> List[Asset]:
        filters = {"business_unit_id": unit_id} if unit_id else {}
        return sorted(self.repo.list(Asset, **filters), key=lambda a: a.name)

    def update_asset(self, asset_id: str, **changes: Any) -> Asset:
        return self._update(Asset, asset_id, changes)

    def delete_asset(self, asset_id: str) -> None:
        self._delete(Asset, asset_id)

    def create_person(self, name: str, title: Optional[str] = None, email: Optional[str] = None) -> Person:
        return self._create(Person, name=name, title=title, email=email)

    def get_person(self, person_id: str) -> Person:
        return self._require(Person, person_id, "Person")

    def list_persons(self) -> List[Person]:
        return sorted(self.repo.list(Person), key=lambda p: p.name)

    def update_person(self, person_id: str, **changes: Any) -> Person:
        return self._update(Person, person_id, changes)

    def delete_person(self, person_id: str) -> None:
        """Deletes a person; they are removed as risk owner, delegate and owner of risks/controls."""
        self._delete(Person, person_id)

    # =====================================================================
    # Risks
    # =====================================================================

    def create_risk(self, unit_id: str, name: str, frequency: Dict[str, Any], impact: Dict[str, Any],
                    **optional: Any) -> Risk:
        """Optional: description, category, asset_ids, owner_id, residual_frequency, residual_impact,
        service_ids, supplier_ids."""
        return self._create(Risk, name=name, business_unit_id=unit_id, frequency=frequency, impact=impact,
                            **optional)

    def get_risk(self, risk_id: str) -> Risk:
        return self._require(Risk, risk_id, "Risk")

    def list_risks(self, unit_id: Optional[str] = None, category: Optional[str] = None,
                   include_children: bool = False) -> List[Risk]:
        risks = self.repo.list(Risk, **({"category": category} if category else {}))
        if unit_id:
            units = set(self._descendant_ids(unit_id) if include_children else [unit_id])
            risks = [r for r in risks if r.business_unit_id in units]
        return sorted(risks, key=lambda r: r.name)

    def update_risk(self, risk_id: str, **changes: Any) -> Risk:
        return self._update(Risk, risk_id, changes)

    def delete_risk(self, risk_id: str) -> None:
        """Deletes the risk and its events, and unlinks it from controls."""
        self._delete(Risk, risk_id)

    def risk_scores(self, risk: Risk, graph: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Derived 1-5 scores and key figures, inherent and residual (incl. outage consequences)."""
        s = self.scoring
        if risk.outage_hours or risk.residual_outage_hours:
            graph = graph or self._graph()
        inherent = risk_model.risk_scores(risk.frequency, self.risk_severity(risk, False, graph),
                                          s.likelihood_bands, s.impact_bands)
        residual = risk_model.risk_scores(risk.residual_frequency or risk.frequency,
                                          self.risk_severity(risk, True, graph),
                                          s.likelihood_bands, s.impact_bands)
        return {"inherent": inherent, "residual": residual}

    def risk_view(self, risk: Risk, controls_by_risk: Optional[Dict[str, List[str]]] = None,
                  graph: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A risk as presented to clients: stored fields + derived scores + linked controls."""
        if controls_by_risk is None:
            controls_by_risk = self._controls_by_risk()
        return {**risk.to_dict(), "scores": self.risk_scores(risk, graph),
                "control_ids": controls_by_risk.get(risk.id, [])}

    def _controls_by_risk(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = defaultdict(list)
        for c in self.repo.list(Control):
            for rid in c.risk_ids:
                out[rid].append(c.id)
        return out

    def list_risk_views(self, **filters: Any) -> List[Dict[str, Any]]:
        cbr = self._controls_by_risk()
        risks = self.list_risks(**filters)
        graph = self._graph() if any(r.outage_hours or r.residual_outage_hours for r in risks) else None
        return [self.risk_view(r, cbr, graph) for r in risks]

    # =====================================================================
    # Controls
    # =====================================================================

    def create_control(self, unit_id: str, name: str, **optional: Any) -> Control:
        """Optional: description, control_type, implementation_status,
        effectiveness_rating, owner_id, risk_ids."""
        return self._create(Control, name=name, business_unit_id=unit_id, **optional)

    def get_control(self, control_id: str) -> Control:
        return self._require(Control, control_id, "Control")

    def list_controls(self, unit_id: Optional[str] = None, risk_id: Optional[str] = None) -> List[Control]:
        controls = self.repo.list(Control, **({"business_unit_id": unit_id} if unit_id else {}))
        if risk_id:
            controls = [c for c in controls if risk_id in c.risk_ids]
        return sorted(controls, key=lambda c: c.name)

    def update_control(self, control_id: str, **changes: Any) -> Control:
        return self._update(Control, control_id, changes)

    def delete_control(self, control_id: str) -> None:
        """Deletes the control and its test events."""
        self._delete(Control, control_id)

    def latest_control_tests(self) -> Dict[str, ControlEvent]:
        latest: Dict[str, ControlEvent] = {}
        for ev in self.repo.list(ControlEvent):
            if ev.status == "IN_PROGRESS":
                continue
            current = latest.get(ev.control_id)
            if current is None or (ev.date, ev.created_at or "") > (current.date, current.created_at or ""):
                latest[ev.control_id] = ev
        return latest

    # =====================================================================
    # Risk events and control events
    # =====================================================================

    def create_risk_event(self, risk_id: str, name: str, date: str, **optional: Any) -> RiskEvent:
        """Optional: event_type, status, description, impact_description, financial_impact."""
        return self._create(RiskEvent, risk_id=risk_id, name=name, date=date, **optional)

    def get_risk_event(self, event_id: str) -> RiskEvent:
        return self._require(RiskEvent, event_id, "Risk event")

    def list_risk_events(self, risk_id: Optional[str] = None, unit_id: Optional[str] = None) -> List[RiskEvent]:
        events = self.repo.list(RiskEvent, **({"risk_id": risk_id} if risk_id else {}))
        if unit_id:
            risk_ids = {r.id for r in self.list_risks(unit_id, include_children=True)}
            events = [e for e in events if e.risk_id in risk_ids]
        return sorted(events, key=lambda e: e.date, reverse=True)

    def update_risk_event(self, event_id: str, **changes: Any) -> RiskEvent:
        return self._update(RiskEvent, event_id, changes)

    def delete_risk_event(self, event_id: str) -> None:
        self._delete(RiskEvent, event_id)

    def create_control_event(self, control_id: str, date: str, **optional: Any) -> ControlEvent:
        """Optional: event_type, status, description."""
        return self._create(ControlEvent, control_id=control_id, date=date, **optional)

    def get_control_event(self, event_id: str) -> ControlEvent:
        return self._require(ControlEvent, event_id, "Control event")

    def list_control_events(self, control_id: Optional[str] = None, unit_id: Optional[str] = None,
                            status: Optional[str] = None) -> List[ControlEvent]:
        filters = {k: v for k, v in (("control_id", control_id), ("status", status)) if v}
        events = self.repo.list(ControlEvent, **filters)
        if unit_id:
            units = set(self._descendant_ids(unit_id))
            control_ids = {c.id for c in self.repo.list(Control) if c.business_unit_id in units}
            events = [e for e in events if e.control_id in control_ids]
        return sorted(events, key=lambda e: e.date, reverse=True)

    def update_control_event(self, event_id: str, **changes: Any) -> ControlEvent:
        return self._update(ControlEvent, event_id, changes)

    def delete_control_event(self, event_id: str) -> None:
        self._delete(ControlEvent, event_id)

    # =====================================================================
    # Audit log
    # =====================================================================

    def list_audit(self, limit: int = 1000, entity_type: Optional[str] = None,
                   action: Optional[str] = None, entity_id: Optional[str] = None) -> List[AuditEntry]:
        """Newest first. With entity_id: the full history of one entity."""
        filters = {k: v for k, v in (("entity_type", entity_type), ("action", action),
                                     ("entity_id", entity_id)) if v}
        entries = sorted(self.repo.list(AuditEntry, **filters), key=lambda a: a.seq, reverse=True)
        return entries[:max(1, min(limit, 10_000))]

    # =====================================================================
    # Quantification
    # =====================================================================

    def simulate(self, n_trials: int = 10_000, seed: Optional[int] = None,
                 residual: bool = True, unit_id: Optional[str] = None) -> risk_model.SimulationResult:
        """Monte Carlo simulation of all risks (or those under unit_id)."""
        if not 100 <= n_trials <= 1_000_000:
            raise ValidationError("n_trials must be between 100 and 1,000,000.")
        risks = self.list_risks(unit_id, include_children=True) if unit_id else self.repo.list(Risk)
        if not risks:
            raise ValidationError("There are no risks to simulate.")
        units = {u.id: u.to_dict() for u in self.repo.list(BusinessUnit)}
        return risk_model.run_simulation(self.engine_risks(risks), units, n_trials=n_trials, seed=seed,
                                         residual=residual,
                                         correlations=self.correlation_pairs([r.id for r in risks]))

    def simulation_report(self, n_trials: int = 10_000, seed: Optional[int] = None,
                          residual: bool = True, unit_id: Optional[str] = None,
                          engine: str = "analytic") -> Dict[str, Any]:
        """
        Loss-distribution report for all risks (or those under unit_id).

        engine="analytic": exact distributions via FFT (fast, no sampling noise,
        n_trials/seed are ignored). engine="monte_carlo": simulation, needed once
        risks are correlated.
        """
        if engine == "analytic":
            risks = self.list_risks(unit_id, include_children=True) if unit_id else self.repo.list(Risk)
            if not risks:
                raise ValidationError("There are no risks to analyse.")
            units = {u.id: u.to_dict() for u in self.repo.list(BusinessUnit)}
            try:
                report = analytic.analyse(self.engine_risks(risks), units, residual=residual,
                                          appetites=self.appetites_for(unit_id),
                                          correlated=len(self.correlation_pairs([r.id for r in risks])))
            except ValueError as e:
                raise ValidationError(f"{e} (try engine='monte_carlo')") from e
        elif engine == "monte_carlo":
            report = self.simulate(n_trials, seed, residual, unit_id).report(appetites=self.appetites_for(unit_id))
            report["engine"] = "monte_carlo"
        else:
            raise ValidationError("engine must be 'analytic' or 'monte_carlo'.")
        report["residual"] = residual
        return report

    def compare_inherent_residual(self, n_trials: int = 10_000, seed: int = 42,
                                  unit_id: Optional[str] = None, engine: str = "analytic") -> Dict[str, Any]:
        inherent = self.simulation_report(n_trials, seed, residual=False, unit_id=unit_id, engine=engine)
        residual = self.simulation_report(n_trials, seed, residual=True, unit_id=unit_id, engine=engine)
        return {
            "inherent": inherent,
            "residual": residual,
            "reduction_in_expected_loss": inherent["total"]["mean"] - residual["total"]["mean"],
            "reduction_in_p99": inherent["total"]["p99"] - residual["total"]["p99"],
        }

    # =====================================================================
    # Analysis runs: stored results and the run register
    # =====================================================================

    def _analysis_inputs(self, unit_id: Optional[str]) -> Dict[str, Any]:
        """Exactly the data a report depends on (and nothing else)."""
        risks = self.list_risks(unit_id, include_children=True) if unit_id else self.repo.list(Risk)
        fields = ("id", "name", "business_unit_id", "frequency", "impact", "residual_frequency", "residual_impact")
        shock = ("outage_hours", "residual_outage_hours", "consequences")
        engine = self.engine_risks(risks)
        # outage fields only when used, so the fingerprints of runs made before they existed stay valid
        rows = [{**{f: getattr(r, f) for f in fields}, **{f: engine[r.id][f] for f in shock if engine[r.id].get(f)}}
                for r in risks]
        out = {
            "risks": sorted(rows, key=lambda r: r["id"]),
            "units": sorted(({"id": u.id, "name": u.name, "parent_id": u.parent_id}
                             for u in self.repo.list(BusinessUnit)), key=lambda u: u["id"]),
        }
        # only when used, so the fingerprints of runs made before they existed stay valid
        pairs = self.correlation_pairs([r.id for r in risks])
        if pairs:
            out["correlations"] = [list(p) for p in pairs]
        appetites = self.appetites_for(unit_id)
        if appetites:
            out["appetites"] = appetites
        return out

    @staticmethod
    def _run_params(mode: str, engine: str, n_trials: int, seed: Optional[int], residual: bool,
                    unit_id: Optional[str]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"mode": mode, "engine": engine, "unit_id": unit_id}
        if mode == "single":
            params["residual"] = residual
        if engine == "monte_carlo":
            params.update(n_trials=n_trials, seed=seed, engine_version=risk_model.ENGINE_VERSION)
        else:
            params.update(grid_points=analytic.DEFAULT_POINTS, engine_version=analytic.ENGINE_VERSION)
        return params

    def analysis_fingerprint(self, params: Dict[str, Any]) -> str:
        return audit.sha256({"params": params, "inputs": self._analysis_inputs(params.get("unit_id"))})

    def run_analysis(self, mode: str = "compare", engine: str = "analytic", n_trials: int = 10_000,
                     seed: Optional[int] = 42, residual: bool = True, unit_id: Optional[str] = None,
                     use_cache: bool = True, label: Optional[str] = None) -> Dict[str, Any]:
        """
        Returns the report for these settings. If an earlier run has the same
        fingerprint (same data, engine and settings) its stored report is
        returned instantly; otherwise it is calculated and stored as a new run.
        The report carries a "run" block saying which run it is and whether it
        was cached. Monte Carlo without a seed is never reused (not repeatable).
        """
        if mode not in ("single", "compare"):
            raise ValidationError("mode must be 'single' or 'compare'.")
        if engine not in ("analytic", "monte_carlo"):
            raise ValidationError("engine must be 'analytic' or 'monte_carlo'.")
        params = self._run_params(mode, engine, n_trials, seed, residual, unit_id)
        fingerprint = self.analysis_fingerprint(params)
        repeatable = not (engine == "monte_carlo" and seed is None)

        if use_cache and repeatable:
            previous = sorted(self.repo.list(AnalysisRun, fingerprint=fingerprint), key=lambda r: r.number)
            if previous:
                run = previous[-1]
                if label and run.label != label:
                    run = self.label_run(run.id, label)
                return {**run.report, "run": self.run_meta(run, cached=True, fingerprint=fingerprint)}

        started = datetime.now(timezone.utc)
        if mode == "compare":
            report = self.compare_inherent_residual(n_trials, seed if seed is not None else 42, unit_id, engine)
        else:
            report = self.simulation_report(n_trials, seed, residual, unit_id, engine)
        duration = (datetime.now(timezone.utc) - started).total_seconds() * 1000

        run = self._store_run(fingerprint, mode, engine, params, report, round(duration, 1), label)
        return {**report, "run": self.run_meta(run, cached=False, fingerprint=fingerprint)}

    def _store_run(self, fingerprint: str, mode: str, engine: str, params: Dict[str, Any],
                   report: Dict[str, Any], duration_ms: float, label: Optional[str]) -> AnalysisRun:
        """Writes the run (under the write lock; the calculation itself ran outside it)."""
        head_seq, head_hash = self._chain_head()
        last = self.repo.latest(AnalysisRun, "number")
        run = AnalysisRun(number=(last.number if last else 0) + 1, fingerprint=fingerprint,
                          mode=mode, engine=engine, params=params, report=report, created_by=self.actor,
                          package_version=_package_version(), audit_seq=head_seq, audit_hash=head_hash,
                          duration_ms=duration_ms, label=label or None)
        run.created_at = run.updated_at = _now()
        self.repo.save(run)
        self._audit("RUN", run.kind, run.id,
                    f"run #{run.number}: {mode} {engine}" + (f" '{label}'" if label else ""),
                    changes={"fingerprint": [None, fingerprint]})
        return run

    def run_meta(self, run: AnalysisRun, cached: Optional[bool] = None,
                  fingerprint: Optional[str] = None) -> Dict[str, Any]:
        """Run information without the (large) report."""
        current = fingerprint or self.analysis_fingerprint(run.params)
        meta = {k: v for k, v in run.to_dict().items() if k != "report"}
        meta["stale"] = current != run.fingerprint  # the data changed since this run
        if cached is not None:
            meta["cached"] = cached
        return meta

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Newest first, without reports, each marked stale if the data has changed since."""
        runs = sorted(self.repo.list(AnalysisRun), key=lambda r: r.number, reverse=True)[:max(1, limit)]
        fingerprints: Dict[str, str] = {}
        out = []
        for run in runs:
            key = audit.canonical(run.params)
            if key not in fingerprints:
                fingerprints[key] = self.analysis_fingerprint(run.params)
            out.append(self.run_meta(run, fingerprint=fingerprints[key]))
        return out

    def get_run(self, run_id: str) -> Dict[str, Any]:
        run = self._require(AnalysisRun, run_id, "Analysis run")
        return {**run.report, "run": self.run_meta(run)}

    def label_run(self, run_id: str, label: Optional[str]) -> AnalysisRun:
        """Names a run, e.g. the figures presented at a board meeting."""
        run = self._require(AnalysisRun, run_id, "Analysis run")
        old, run.label = run.label, (label or "").strip()[:200] or None
        run.updated_at = _now()
        self.repo.save(run)
        self._audit("LABEL", run.kind, run.id, f"run #{run.number}", changes={"label": [old, run.label]})
        return run

    def reproduce_run(self, run_id: str) -> Dict[str, Any]:
        """
        Recalculates a run from the current data and compares it with the
        stored report. Only meaningful while the run is not stale.
        """
        run = self._require(AnalysisRun, run_id, "Analysis run")
        meta = self.run_meta(run)
        if meta["stale"]:
            return {"run": meta, "reproducible": None,
                    "message": "The data has changed since this run, so it cannot be recalculated from the "
                               "current data. The stored report is still what was reported at the time."}
        p = run.params
        if p["mode"] == "compare":
            fresh = self.compare_inherent_residual(p.get("n_trials", 10_000), p.get("seed") or 42,
                                                   p.get("unit_id"), p["engine"])
        else:
            fresh = self.simulation_report(p.get("n_trials", 10_000), p.get("seed"), p.get("residual", True),
                                           p.get("unit_id"), p["engine"])
        same = audit.sha256(fresh) == audit.sha256(run.report)
        return {"run": meta, "reproducible": same,
                "message": "Recalculation gives exactly the stored result." if same else
                           "Recalculation differs from the stored result (e.g. a different package version)."}

    # =====================================================================
    # Overviews for dashboards
    # =====================================================================

    def control_effectiveness(self) -> Optional[float]:
        """
        0-100. Based on the latest completed test of each tested control
        (PASSED=1, PARTIAL=0.5, FAILED=0); if no control has been tested,
        on the average effectiveness_rating of implemented controls.
        None when there is nothing to base it on.
        """
        tests = self.latest_control_tests()
        if tests:
            return 100 * sum(CONTROL_TEST_SCORE[t.status] for t in tests.values()) / len(tests)
        ratings = [c.effectiveness_rating for c in self.repo.list(Control)
                   if c.implementation_status == "IMPLEMENTED" and c.effectiveness_rating]
        return 100 * sum(ratings) / (5 * len(ratings)) if ratings else None

    @staticmethod
    def _month_keys(months: int, today: Optional[date] = None) -> List[str]:
        today = today or datetime.now(timezone.utc).date()
        keys, y, m = [], today.year, today.month
        for _ in range(months):
            keys.append(f"{y:04d}-{m:02d}")
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        return list(reversed(keys))

    def dashboard_summary(self, months: int = 6) -> Dict[str, Any]:
        """Simulation-free figures for an overview page."""
        risks = self.repo.list(Risk)
        units = {u.id: u for u in self.repo.list(BusinessUnit)}
        controls = self.repo.list(Control)
        graph = self._graph()
        views = {r.id: self.risk_scores(r, graph) for r in risks}
        inherent_scores = [v["inherent"]["score"] for v in views.values()]
        residual_scores = [v["residual"]["score"] for v in views.values()]
        mitigated = {rid for c in controls for rid in c.risk_ids}

        severity = Counter(v["inherent"]["level"] for v in views.values())
        per_unit_count = Counter(r.business_unit_id for r in risks)
        per_unit_eal: Dict[str, float] = defaultdict(float)
        for r in risks:
            per_unit_eal[r.business_unit_id] += views[r.id]["residual"]["expected_annual_loss"]

        keys = self._month_keys(months)
        created = Counter((r.created_at or "")[:7] for r in risks)
        before_window = sum(n for k, n in created.items() if k and k < keys[0])
        cumulative, running = [], before_window
        for k in keys:
            running += created.get(k, 0)
            cumulative.append(running)
        prev = cumulative[-2] if len(cumulative) > 1 else 0
        trend_pct = (cumulative[-1] - prev) / prev * 100 if prev else 0.0

        effectiveness = self.control_effectiveness()
        return {
            "counts": {
                "business_units": len(units),
                "assets": len(self.repo.list(Asset)),
                "persons": len(self.repo.list(Person)),
                "risks": len(risks),
                "controls": len(controls),
                "controls_implemented": sum(1 for c in controls if c.implementation_status == "IMPLEMENTED"),
                "risks_with_controls": sum(1 for r in risks if r.id in mitigated),
                "risks_without_controls": sum(1 for r in risks if r.id not in mitigated),
                "high_priority_risks": sum(1 for s in inherent_scores if s >= 9),
                "open_risk_events": sum(1 for e in self.repo.list(RiskEvent) if e.status != "CLOSED"),
                "units_without_risk_owner": sum(1 for u in units.values() if not u.risk_owner_id),
            },
            "average_inherent_score": sum(inherent_scores) / len(inherent_scores) if risks else 0.0,
            "average_residual_score": sum(residual_scores) / len(residual_scores) if risks else 0.0,
            "control_effectiveness": effectiveness,
            "risks_by_category": dict(Counter(r.category or "uncategorised" for r in risks).most_common()),
            "risks_by_severity": [{"level": lvl, "count": severity.get(lvl, 0)}
                                  for lvl in reversed(risk_model.SEVERITY_LEVELS)],
            "risk_trend": [{"month": k, "new": created.get(k, 0), "total": t} for k, t in zip(keys, cumulative)],
            "risk_trend_pct": trend_pct,
            "top_units_by_risk_count": [
                {"id": uid, "name": units[uid].name if uid in units else None, "count": n}
                for uid, n in per_unit_count.most_common(5)
            ],
            "expected_annual_loss_total": sum(per_unit_eal.values()),
            "top_risks": [
                {"id": r.id, "name": r.name, "expected_annual_loss": views[r.id]["residual"]["expected_annual_loss"]}
                for r in sorted(risks, key=lambda r: views[r.id]["residual"]["expected_annual_loss"],
                                reverse=True)[:5]
            ],
            "expected_annual_loss_by_unit": [
                {"id": uid, "name": units[uid].name if uid in units else None, "expected_annual_loss": v}
                for uid, v in sorted(per_unit_eal.items(), key=lambda kv: kv[1], reverse=True)
            ],
        }

    def unit_risk_summary(self) -> List[Dict[str, Any]]:
        """Per business unit: risk count, average scores, high risks, EAL (own risks only)."""
        risks_by_unit: Dict[str, List[Risk]] = defaultdict(list)
        for r in self.repo.list(Risk):
            risks_by_unit[r.business_unit_id].append(r)
        out = []
        graph = self._graph()
        for u in self.list_business_units():
            scores = [self.risk_scores(r, graph) for r in risks_by_unit.get(u.id, [])]
            n = len(scores)
            out.append({
                "id": u.id, "name": u.name, "code": u.code, "parent_id": u.parent_id,
                "organization_type": u.organization_type, "description": u.description,
                "risk_count": n,
                "average_inherent_score": sum(s["inherent"]["score"] for s in scores) / n if n else 0.0,
                "average_residual_score": sum(s["residual"]["score"] for s in scores) / n if n else 0.0,
                "high_risk_count": sum(1 for s in scores if s["inherent"]["score"] >= 9),
                "expected_annual_loss": sum(s["residual"]["expected_annual_loss"] for s in scores),
            })
        return out

    # =====================================================================
    # Import / export (backend independent, IDs are preserved)
    # =====================================================================

    def export_data(self) -> Dict[str, Any]:
        return {"version": EXPORT_VERSION,
                **{cls.kind + "s": [e.to_dict() for e in self.repo.list(cls)] for cls in DATA_TYPES}}

    def import_data(self, data: Dict[str, Any], replace: bool = True) -> Dict[str, int]:
        """
        Imports an export (or any subset of its lists). Everything is parsed
        and validated before anything is written; references must point to
        entities in the file or (with replace=False) already stored.
        With replace=True existing data is cleared first.
        """
        try:
            batch = {cls: [cls.from_dict(d) for d in data.get(cls.kind + "s", [])] for cls in DATA_TYPES}
        except (TypeError, KeyError, AttributeError) as e:
            raise ValidationError(f"Invalid import file: {e}") from e

        for items in batch.values():
            for entity in items:
                self._validate(entity)

        known = {cls: ({e.id for e in items} if replace else {e.id for e in items} | {e.id for e in self.repo.list(cls)})
                 for cls, items in batch.items()}
        for items in batch.values():
            for entity in items:
                try:
                    self._check_refs(entity, known)
                except NotFoundError as e:
                    raise ValidationError(f"Import refers to a missing entity: {e}") from e

        units = batch[BusinessUnit] + ([] if replace else
                                       [u for u in self.repo.list(BusinessUnit)
                                        if u.id not in {b.id for b in batch[BusinessUnit]}])
        names = Counter(u.name for u in units)
        dupes = [n for n, c in names.items() if c > 1]
        if dupes:
            raise ConflictError(f"Duplicate business unit names: {', '.join(dupes)}")
        for u in batch[BusinessUnit]:
            if u.parent_id and u.parent_id in self._descendant_ids(u.id, units):
                raise ValidationError(f"Business unit '{u.name}' is placed below itself.")

        # Write phase: one audit entry per entity that actually changes.
        incoming = {cls: {e.id for e in items} for cls, items in batch.items()}
        if replace:  # remove what is not in the file (the audit log itself is kept)
            for cls in reversed(DATA_TYPES):
                for e in self.repo.list(cls):
                    if e.id not in incoming[cls]:
                        self._remove(e, "removed by import (replace)")
        now = _now()
        for cls in DATA_TYPES:
            for entity in batch[cls]:
                existing = self.repo.get(cls, entity.id)
                entity.created_at = entity.created_at or (existing.created_at if existing else None) or now
                entity.updated_at = entity.updated_at or now
                if existing is not None and not audit.diff(existing, entity):
                    continue  # unchanged
                self._save(entity, "UPDATE" if existing else "CREATE", existing,
                           f"imported: {getattr(entity, 'name', entity.id)}", stamp=False)
        counts = {cls.kind: len(items) for cls, items in batch.items()}
        self._audit("IMPORT", "data", None,
                    ("replace " if replace else "merge ") + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
        return counts


# ---------------------------------------------------------------------------
# Write operations run in one storage transaction holding the write lock.
# The audit chain head is re-read under the lock, so several processes (API
# workers, GUI, CLI) on the same database append to one linear chain, and a
# failed operation leaves neither half-saved data nor a dangling audit entry.
# ---------------------------------------------------------------------------

WRITE_METHODS = (
    "create_business_unit", "update_business_unit", "delete_business_unit", "assign_risk_owner",
    "add_delegate", "remove_delegate", "create_asset", "update_asset", "delete_asset",
    "create_person", "update_person", "delete_person", "create_risk", "update_risk", "delete_risk",
    "create_control", "update_control", "delete_control", "create_risk_event", "update_risk_event",
    "delete_risk_event", "create_control_event", "update_control_event", "delete_control_event",
    "import_data", "label_run", "reset_all", "_store_run",
) + (USER_WRITE_METHODS + ANCHOR_WRITE_METHODS + REPORTING_WRITE_METHODS + THIRD_PARTY_WRITE_METHODS
     + PORTFOLIO_WRITE_METHODS + INCIDENT_WRITE_METHODS)


def _write(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self._tx_depth:  # already inside a write (nested call)
            return method(self, *args, **kwargs)
        with self.repo.transaction():
            self._tx_depth += 1
            self._head = None  # another process may have appended: re-read under the lock
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                self._head = None  # the transaction is rolled back, so is our view of the chain
                raise
            finally:
                self._tx_depth -= 1
    return wrapper


for _name in WRITE_METHODS:
    setattr(RiskService, _name, _write(getattr(RiskService, _name)))
