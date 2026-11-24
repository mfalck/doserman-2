"""
Business tier. All rules live here: validation, references between
entities, cascades on delete, audit trail, derived scores, import/export
and the quantitative model.

It depends only on the `Repository` interface, so it works unchanged with
the JSON, SQL and Neo4j backends, and it knows nothing about HTTP or GUIs.
"""

import logging
import typing
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Type

from ..domain import risk_model
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..domain.models import (CONTROL_EVENT_STATUSES, CONTROL_EVENT_TYPES, CONTROL_TYPES, DATA_TYPES,
                             IMPLEMENTATION_STATUSES, ORGANIZATION_TYPES, REFERENCES, RISK_EVENT_STATUSES,
                             RISK_EVENT_TYPES, Asset, AuditEntry, BusinessUnit, Control, ControlEvent, Entity,
                             Person, Risk, RiskEvent)
from ..storage.base import E, Repository

log = logging.getLogger(__name__)

EXPORT_VERSION = "3.0"
CONTROL_TEST_SCORE = {"PASSED": 1.0, "PARTIAL": 0.5, "FAILED": 0.0}


@dataclass
class ScoringScale:
    """Thresholds that turn quantitative specs into 1-5 scores."""
    likelihood_bands: tuple = risk_model.DEFAULT_LIKELIHOOD_BANDS  # P(>=1 event / year)
    impact_bands: tuple = risk_model.DEFAULT_IMPACT_BANDS          # median loss per event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_optional(cls: Type[Entity], field_name: str) -> bool:
    return type(None) in typing.get_args(typing.get_type_hints(cls)[field_name])


class RiskService:

    def __init__(self, repo: Repository, scoring: Optional[ScoringScale] = None, actor: str = "system"):
        self.repo = repo
        self.scoring = scoring or ScoringScale()
        self.actor = actor  # who is making changes; recorded in the audit log
        self._last_audit_ts: Optional[datetime] = None

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

    def _audit(self, action: str, entity_type: str, entity_id: Optional[str], details: Optional[str] = None):
        # Microsecond timestamps, forced strictly increasing so the log order is exact.
        ts = datetime.now(timezone.utc)
        if self._last_audit_ts and ts <= self._last_audit_ts:
            ts = self._last_audit_ts + timedelta(microseconds=1)
        self._last_audit_ts = ts
        self.repo.save(AuditEntry(action=action, entity_type=entity_type, entity_id=entity_id,
                                  details=details, user_id=self.actor,
                                  timestamp=ts.isoformat(timespec="microseconds")))

    def _save(self, entity: Entity, action: str, details: Optional[str] = None) -> None:
        now = _now()
        entity.created_at = entity.created_at or now
        entity.updated_at = now
        self.repo.save(entity)
        self._audit(action, entity.kind, entity.id, details or getattr(entity, "name", None))

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
        unknown = set(changes) - (set(cls.field_names()) - {"id", "created_at", "updated_at"})
        if unknown:
            raise ValidationError(f"Unknown fields for {cls.__name__}: {', '.join(sorted(unknown))}")
        for key, value in changes.items():
            setattr(entity, key, value)
        self._validate(entity)
        self._check_refs(entity)
        self._save(entity, "UPDATE", "changed: " + ", ".join(sorted(changes)) if changes else None)
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
                if is_list and entity_id in value:
                    setattr(ref, field_name, [v for v in value if v != entity_id])
                    self._save(ref, "UPDATE", f"removed reference to deleted {cls.kind} {entity_id}")
                elif not is_list and value == entity_id:
                    if _is_optional(src, field_name):
                        setattr(ref, field_name, None)
                        self._save(ref, "UPDATE", f"cleared {field_name} (deleted {cls.kind})")
                    else:
                        self._delete(src, ref.id)
        self.repo.delete(cls, entity_id)
        self._audit("DELETE", cls.kind, entity_id, getattr(entity, "name", None))

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
            try:
                risk_model.validate_frequency(e.frequency)
                risk_model.build_impact_distribution(e.impact)
                if e.residual_frequency:
                    risk_model.validate_frequency(e.residual_frequency)
                if e.residual_impact:
                    risk_model.build_impact_distribution(e.residual_impact)
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
        """Optional: description, category, asset_ids, owner_id, residual_frequency, residual_impact."""
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

    def risk_scores(self, risk: Risk) -> Dict[str, Any]:
        """Derived 1-5 scores and key figures, inherent and residual."""
        s = self.scoring
        inherent = risk_model.risk_scores(risk.frequency, risk.impact, s.likelihood_bands, s.impact_bands)
        residual = risk_model.risk_scores(risk.residual_frequency or risk.frequency,
                                          risk.residual_impact or risk.impact,
                                          s.likelihood_bands, s.impact_bands)
        return {"inherent": inherent, "residual": residual}

    def risk_view(self, risk: Risk, controls_by_risk: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
        """A risk as presented to clients: stored fields + derived scores + linked controls."""
        if controls_by_risk is None:
            controls_by_risk = self._controls_by_risk()
        return {**risk.to_dict(), "scores": self.risk_scores(risk),
                "control_ids": controls_by_risk.get(risk.id, [])}

    def _controls_by_risk(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = defaultdict(list)
        for c in self.repo.list(Control):
            for rid in c.risk_ids:
                out[rid].append(c.id)
        return out

    def list_risk_views(self, **filters: Any) -> List[Dict[str, Any]]:
        cbr = self._controls_by_risk()
        return [self.risk_view(r, cbr) for r in self.list_risks(**filters)]

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
                   action: Optional[str] = None) -> List[AuditEntry]:
        filters = {k: v for k, v in (("entity_type", entity_type), ("action", action)) if v}
        entries = sorted(self.repo.list(AuditEntry, **filters), key=lambda a: a.timestamp, reverse=True)
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
        return risk_model.run_simulation({r.id: r.to_dict() for r in risks}, units,
                                         n_trials=n_trials, seed=seed, residual=residual)

    def simulation_report(self, n_trials: int = 10_000, seed: Optional[int] = None,
                          residual: bool = True, unit_id: Optional[str] = None) -> Dict[str, Any]:
        report = self.simulate(n_trials, seed, residual, unit_id).report()
        report["residual"] = residual
        return report

    def compare_inherent_residual(self, n_trials: int = 10_000, seed: int = 42,
                                  unit_id: Optional[str] = None) -> Dict[str, Any]:
        inherent = self.simulation_report(n_trials, seed, residual=False, unit_id=unit_id)
        residual = self.simulation_report(n_trials, seed, residual=True, unit_id=unit_id)
        return {
            "inherent": inherent,
            "residual": residual,
            "reduction_in_expected_loss": inherent["total"]["mean"] - residual["total"]["mean"],
            "reduction_in_p99": inherent["total"]["p99"] - residual["total"]["p99"],
        }

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
        views = {r.id: self.risk_scores(r) for r in risks}
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
        for u in self.list_business_units():
            scores = [self.risk_scores(r) for r in risks_by_unit.get(u.id, [])]
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

        if replace:
            for cls in DATA_TYPES:  # keep the audit log
                for e in self.repo.list(cls):
                    self.repo.delete(cls, e.id)
        now = _now()
        for cls in DATA_TYPES:
            for entity in batch[cls]:
                entity.created_at = entity.created_at or now
                entity.updated_at = entity.updated_at or now
                self.repo.save(entity)
        counts = {cls.kind: len(items) for cls, items in batch.items()}
        self._audit("IMPORT", "data", None,
                    ("replace " if replace else "merge ") + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
        return counts
