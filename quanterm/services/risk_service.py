"""
Business tier. All rules live here: validation, references between
entities, cascades on delete, import/export and the quantitative model.

It depends only on the `Repository` interface, so it works unchanged with
the JSON, SQL and Neo4j backends, and it knows nothing about HTTP or GUIs.
"""

import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Type

from ..domain import risk_model
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..domain.models import Asset, BusinessUnit, Entity, Person, Risk
from ..storage.base import E, Repository

log = logging.getLogger(__name__)

EXPORT_VERSION = "2.0"


class RiskService:

    def __init__(self, repo: Repository):
        self.repo = repo

    # =====================================================================
    # Generic helpers
    # =====================================================================

    def _require(self, cls: Type[E], entity_id: Optional[str], what: Optional[str] = None) -> E:
        if not entity_id:
            raise ValidationError(f"{what or cls.__name__} ID cannot be empty.")
        entity = self.repo.get(cls, entity_id)
        if entity is None:
            raise NotFoundError(f"{what or cls.__name__} '{entity_id}' not found.")
        return entity

    @staticmethod
    def _apply_changes(entity: Entity, changes: Dict[str, Any]) -> None:
        unknown = set(changes) - (set(entity.field_names()) - {"id"})
        if unknown:
            raise ValidationError(f"Unknown fields for {type(entity).__name__}: {', '.join(sorted(unknown))}")
        for key, value in changes.items():
            setattr(entity, key, value)

    @staticmethod
    def _require_name(name: Optional[str], what: str) -> str:
        if not name or not str(name).strip():
            raise ValidationError(f"{what} name cannot be empty.")
        return str(name).strip()

    # =====================================================================
    # Business units
    # =====================================================================

    def _check_unit_name_unique(self, name: str, exclude_id: Optional[str] = None) -> None:
        for bu in self.repo.list(BusinessUnit, name=name):
            if bu.id != exclude_id:
                raise ConflictError(f"Business unit with name '{name}' already exists.")

    def _descendant_ids(self, unit_id: str) -> List[str]:
        """unit_id and all units below it (depth first)."""
        children: Dict[Optional[str], List[str]] = {}
        for bu in self.repo.list(BusinessUnit):
            children.setdefault(bu.parent_id, []).append(bu.id)
        out, stack = [], [unit_id]
        while stack:
            current = stack.pop()
            if current not in out:
                out.append(current)
                stack.extend(children.get(current, []))
        return out

    def create_business_unit(self, name: str, description: Optional[str] = None,
                             parent_id: Optional[str] = None) -> BusinessUnit:
        name = self._require_name(name, "Business unit")
        self._check_unit_name_unique(name)
        if parent_id:
            self._require(BusinessUnit, parent_id, "Parent business unit")
        bu = BusinessUnit(name=name, description=description, parent_id=parent_id)
        self.repo.save(bu)
        log.info("Created %s", bu)
        return bu

    def get_business_unit(self, unit_id: str) -> BusinessUnit:
        return self._require(BusinessUnit, unit_id, "Business unit")

    def list_business_units(self) -> List[BusinessUnit]:
        return sorted(self.repo.list(BusinessUnit), key=lambda b: b.name)

    def update_business_unit(self, unit_id: str, **changes: Any) -> BusinessUnit:
        bu = self.get_business_unit(unit_id)
        if "name" in changes:
            changes["name"] = self._require_name(changes["name"], "Business unit")
            self._check_unit_name_unique(changes["name"], exclude_id=unit_id)
        if changes.get("parent_id"):
            self._require(BusinessUnit, changes["parent_id"], "Parent business unit")
            if changes["parent_id"] in self._descendant_ids(unit_id):
                raise ValidationError("A business unit cannot be moved below itself.")
        if changes.get("risk_owner_id"):
            self._require(Person, changes["risk_owner_id"], "Person")
        for pid in changes.get("delegate_ids") or []:
            self._require(Person, pid, "Person")
        self._apply_changes(bu, changes)
        self.repo.save(bu)
        return bu

    def delete_business_unit(self, unit_id: str, cascade: bool = False) -> List[str]:
        """
        Deletes a unit with its assets and risks. If it has child units,
        `cascade=True` is required and the whole subtree is deleted.
        Returns the IDs of deleted business units.
        """
        self.get_business_unit(unit_id)
        units = self._descendant_ids(unit_id)
        if len(units) > 1 and not cascade:
            raise ConflictError("Business unit has child units; delete with cascade=True.")
        for uid in units:
            for asset in self.repo.list(Asset, business_unit_id=uid):
                self.delete_asset(asset.id)
            for risk in self.repo.list(Risk, business_unit_id=uid):
                self.repo.delete(Risk, risk.id)
            self.repo.delete(BusinessUnit, uid)
        log.info("Deleted business units %s", units)
        return units

    def assign_risk_owner(self, unit_id: str, person_id: Optional[str]) -> BusinessUnit:
        """Sets (or with person_id=None clears) the risk owner."""
        return self.update_business_unit(unit_id, risk_owner_id=person_id)

    def add_delegate(self, unit_id: str, person_id: str) -> BusinessUnit:
        bu = self.get_business_unit(unit_id)
        self._require(Person, person_id, "Person")
        if person_id not in bu.delegate_ids:
            bu.delegate_ids.append(person_id)
            self.repo.save(bu)
        return bu

    def remove_delegate(self, unit_id: str, person_id: str) -> BusinessUnit:
        bu = self.get_business_unit(unit_id)
        if person_id in bu.delegate_ids:
            bu.delegate_ids.remove(person_id)
            self.repo.save(bu)
        return bu

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
    # Assets
    # =====================================================================

    def create_asset(self, unit_id: str, name: str, asset_type: str,
                     description: Optional[str] = None, criticality: Optional[str] = None) -> Asset:
        self.get_business_unit(unit_id)
        name = self._require_name(name, "Asset")
        if not asset_type:
            raise ValidationError("Asset type cannot be empty.")
        asset = Asset(name=name, asset_type=asset_type, business_unit_id=unit_id,
                      description=description, criticality=criticality)
        self.repo.save(asset)
        return asset

    def get_asset(self, asset_id: str) -> Asset:
        return self._require(Asset, asset_id, "Asset")

    def list_assets(self, unit_id: Optional[str] = None) -> List[Asset]:
        filters = {"business_unit_id": unit_id} if unit_id else {}
        return sorted(self.repo.list(Asset, **filters), key=lambda a: a.name)

    def update_asset(self, asset_id: str, **changes: Any) -> Asset:
        asset = self.get_asset(asset_id)
        if "name" in changes:
            changes["name"] = self._require_name(changes["name"], "Asset")
        if "business_unit_id" in changes:
            self.get_business_unit(changes["business_unit_id"])
        self._apply_changes(asset, changes)
        self.repo.save(asset)
        return asset

    def delete_asset(self, asset_id: str) -> None:
        self.get_asset(asset_id)
        for risk in self.repo.list(Risk):
            if asset_id in risk.asset_ids:
                risk.asset_ids.remove(asset_id)
                self.repo.save(risk)
        self.repo.delete(Asset, asset_id)

    # =====================================================================
    # Persons
    # =====================================================================

    def create_person(self, name: str, title: Optional[str] = None, email: Optional[str] = None) -> Person:
        person = Person(name=self._require_name(name, "Person"), title=title, email=email)
        self.repo.save(person)
        return person

    def get_person(self, person_id: str) -> Person:
        return self._require(Person, person_id, "Person")

    def list_persons(self) -> List[Person]:
        return sorted(self.repo.list(Person), key=lambda p: p.name)

    def update_person(self, person_id: str, **changes: Any) -> Person:
        person = self.get_person(person_id)
        if "name" in changes:
            changes["name"] = self._require_name(changes["name"], "Person")
        self._apply_changes(person, changes)
        self.repo.save(person)
        return person

    def delete_person(self, person_id: str) -> None:
        """Deletes a person and removes them as risk owner, delegate and risk owner."""
        self.get_person(person_id)
        for bu in self.repo.list(BusinessUnit):
            if bu.risk_owner_id == person_id or person_id in bu.delegate_ids:
                bu.risk_owner_id = None if bu.risk_owner_id == person_id else bu.risk_owner_id
                bu.delegate_ids = [d for d in bu.delegate_ids if d != person_id]
                self.repo.save(bu)
        for risk in self.repo.list(Risk, owner_id=person_id):
            risk.owner_id = None
            self.repo.save(risk)
        self.repo.delete(Person, person_id)

    # =====================================================================
    # Risks
    # =====================================================================

    def _validate_risk(self, risk: Risk) -> None:
        self._require_name(risk.name, "Risk")
        self.get_business_unit(risk.business_unit_id)
        for asset_id in risk.asset_ids:
            self.get_asset(asset_id)
        if risk.owner_id:
            self.get_person(risk.owner_id)
        try:
            risk_model.validate_frequency(risk.frequency)
            risk_model.build_impact_distribution(risk.impact)
            if risk.residual_frequency:
                risk_model.validate_frequency(risk.residual_frequency)
            if risk.residual_impact:
                risk_model.build_impact_distribution(risk.residual_impact)
        except ValueError as e:
            raise ValidationError(str(e)) from e

    def create_risk(self, unit_id: str, name: str, frequency: Dict[str, Any], impact: Dict[str, Any],
                    **optional: Any) -> Risk:
        """Optional fields: description, category, asset_ids, owner_id,
        residual_frequency, residual_impact, controls."""
        try:
            risk = Risk(name=name, business_unit_id=unit_id, frequency=frequency, impact=impact, **optional)
        except TypeError as e:
            raise ValidationError(str(e)) from e
        risk.asset_ids = list(risk.asset_ids or [])
        risk.controls = list(risk.controls or [])
        self._validate_risk(risk)
        risk.name = risk.name.strip()
        self.repo.save(risk)
        return risk

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
        risk = self.get_risk(risk_id)
        self._apply_changes(risk, changes)
        self._validate_risk(risk)
        self.repo.save(risk)
        return risk

    def delete_risk(self, risk_id: str) -> None:
        self.get_risk(risk_id)
        self.repo.delete(Risk, risk_id)

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

    def compare_inherent_residual(self, n_trials: int = 10_000, seed: int = 42) -> Dict[str, Any]:
        inherent = self.simulation_report(n_trials, seed, residual=False)
        residual = self.simulation_report(n_trials, seed, residual=True)
        return {
            "inherent": inherent,
            "residual": residual,
            "reduction_in_expected_loss": inherent["total"]["mean"] - residual["total"]["mean"],
            "reduction_in_p99": inherent["total"]["p99"] - residual["total"]["p99"],
        }

    def dashboard_summary(self) -> Dict[str, Any]:
        """Fast, simulation-free figures for an overview page."""
        risks = self.repo.list(Risk)
        units = {u.id: u for u in self.repo.list(BusinessUnit)}
        eal = {r.id: risk_model.expected_annual_loss(
            r.residual_frequency or r.frequency, r.residual_impact or r.impact) for r in risks}
        per_unit = Counter()
        for r in risks:
            per_unit[r.business_unit_id] += eal[r.id]
        return {
            "counts": {
                "business_units": len(units),
                "assets": len(self.repo.list(Asset)),
                "persons": len(self.repo.list(Person)),
                "risks": len(risks),
                "risks_with_controls": sum(1 for r in risks if r.controls),
                "units_without_risk_owner": sum(1 for u in units.values() if not u.risk_owner_id),
            },
            "risks_by_category": dict(Counter(r.category or "uncategorised" for r in risks)),
            "expected_annual_loss_total": sum(eal.values()),
            "top_risks": [
                {"id": r.id, "name": r.name, "expected_annual_loss": eal[r.id]}
                for r in sorted(risks, key=lambda r: eal[r.id], reverse=True)[:5]
            ],
            "expected_annual_loss_by_unit": [
                {"id": uid, "name": units[uid].name if uid in units else None, "expected_annual_loss": v}
                for uid, v in per_unit.most_common()
            ],
        }

    # =====================================================================
    # Import / export (backend independent, IDs are preserved)
    # =====================================================================

    def export_data(self) -> Dict[str, Any]:
        return {
            "version": EXPORT_VERSION,
            "business_units": [e.to_dict() for e in self.repo.list(BusinessUnit)],
            "assets": [e.to_dict() for e in self.repo.list(Asset)],
            "persons": [e.to_dict() for e in self.repo.list(Person)],
            "risks": [e.to_dict() for e in self.repo.list(Risk)],
        }

    def import_data(self, data: Dict[str, Any], replace: bool = True) -> Dict[str, int]:
        """
        Imports an export. Everything is parsed and validated before any
        data is written; references to unknown IDs are rejected.
        With replace=True existing data is cleared first.
        """
        try:
            batch = {
                Person: [Person.from_dict(d) for d in data.get("persons", [])],
                BusinessUnit: [BusinessUnit.from_dict(d) for d in data.get("business_units", [])],
                Asset: [Asset.from_dict(d) for d in data.get("assets", [])],
                Risk: [Risk.from_dict(d) for d in data.get("risks", [])],
            }
        except (TypeError, KeyError) as e:
            raise ValidationError(f"Invalid import file: {e}") from e

        existing = {cls: set() if replace else {e.id for e in self.repo.list(cls)} for cls in batch}
        ids = {cls: existing[cls] | {e.id for e in items} for cls, items in batch.items()}

        def ref(cls, value, what):
            if value and value not in ids[cls]:
                raise ValidationError(f"Import refers to unknown {what} '{value}'.")

        for bu in batch[BusinessUnit]:
            ref(BusinessUnit, bu.parent_id, "business unit")
            ref(Person, bu.risk_owner_id, "person")
            for d in bu.delegate_ids:
                ref(Person, d, "person")
        for a in batch[Asset]:
            ref(BusinessUnit, a.business_unit_id, "business unit")
        for r in batch[Risk]:
            ref(BusinessUnit, r.business_unit_id, "business unit")
            ref(Person, r.owner_id, "person")
            for a in r.asset_ids:
                ref(Asset, a, "asset")
            try:
                risk_model.validate_frequency(r.frequency)
                risk_model.build_impact_distribution(r.impact)
            except ValueError as e:
                raise ValidationError(f"Risk '{r.name}': {e}") from e

        if replace:
            self.repo.clear()
        for cls in (Person, BusinessUnit, Asset, Risk):
            for entity in batch[cls]:
                self.repo.save(entity)
        return {cls.kind: len(items) for cls, items in batch.items()}
