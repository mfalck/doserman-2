"""
Objectives: what the organisation must achieve, and which risks threaten it.

A risk threatens an objective when it names it (Risk.objective_ids), or when
it can disrupt a service the objective is delivered through - directly
(Risk.service_ids) or through a supplier behind that service, also as a
fourth party (Risk.supplier_ids). The risk to an objective is the annual loss
distribution of those risks together; with a tolerance (at most p chance per
year of losing more than x) the report says whether it is kept.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..domain.errors import ValidationError
from ..domain.models import OBJECTIVE_CRITICALITIES, Objective
from .core import ServiceCore

CHECK_TRIALS = 20_000


class Objectives(ServiceCore):
    """Mixin for RiskService (uses _create/_update/_delete/_require/repo and ThirdParty._graph)."""

    if TYPE_CHECKING:  # members of other service areas this one uses
        def _graph(self) -> Dict[str, Any]: ...
        def _service_ids_hit_by_supplier(self, supplier_id: str, g: Dict[str, Any]) -> Dict[str, int]: ...
        def correlation_pairs(self, risk_ids=None) -> List[Tuple[str, str, float]]: ...
        def simulation_report(self, n_trials: int = 10_000, seed: Optional[int] = None,
                              residual: bool = True, unit_id: Optional[str] = None,
                              engine: str = "analytic") -> Dict[str, Any]: ...


    def _validate_objective(self, e) -> bool:
        if not isinstance(e, Objective):
            return False
        self._name(e, "Objective")
        self._enum(e.criticality, OBJECTIVE_CRITICALITIES, "criticality")
        if e.target_date:
            self._date(e.target_date, "target_date")
        e.service_ids = list(dict.fromkeys(e.service_ids or []))
        self._non_negative(e.tolerance_loss, "tolerance_loss")
        if (e.tolerance_loss is None) != (e.tolerance_probability is None):
            raise ValidationError("Give both the tolerance loss and its probability, or neither.")
        if e.tolerance_probability is not None and not 0 < e.tolerance_probability < 1:
            raise ValidationError("tolerance_probability must be between 0 and 1 (e.g. 0.05 for 5%).")
        return True

    # -- CRUD ---------------------------------------------------------------------------------

    def create_objective(self, name: str, **optional: Any) -> Objective:
        return self._create(Objective, name=name, **optional)

    def get_objective(self, objective_id: str) -> Objective:
        return self._require(Objective, objective_id, "Objective")

    def list_objectives(self) -> List[Objective]:
        rank = {c: i for i, c in enumerate(OBJECTIVE_CRITICALITIES)}
        return sorted(self.repo.list(Objective), key=lambda o: (rank.get(o.criticality, 9), o.name))

    def update_objective(self, objective_id: str, **changes: Any) -> Objective:
        return self._update(Objective, objective_id, changes)

    def delete_objective(self, objective_id: str) -> None:
        """Unlinks it from risks."""
        self._delete(Objective, objective_id)

    def _delete_unit_objectives(self, unit_id: str) -> None:
        """A unit's objectives go with the unit (they must not silently become organisation-wide)."""
        for o in self.repo.list(Objective, business_unit_id=unit_id):
            self._delete(Objective, o.id)

    # -- mapping ------------------------------------------------------------------------------

    def _threats(self, g: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, str]]:
        """objective id -> {risk id: how it threatens it ("direct", "service X", "supplier Y")}."""
        g = g or self._graph()
        out: Dict[str, Dict[str, str]] = {o.id: {} for o in self.repo.list(Objective)}
        objectives = self.repo.list(Objective)
        hit_by_supplier: Dict[str, Dict[str, int]] = {}
        for risk in g["risks"]:
            for oid in risk.objective_ids:
                if oid in out:
                    out[oid][risk.id] = "direct"
            services = {sid: f"service {g['services'][sid].name}" for sid in risk.service_ids if sid in g["services"]}
            for sup in risk.supplier_ids:
                if sup not in hit_by_supplier:
                    hit_by_supplier[sup] = self._service_ids_hit_by_supplier(sup, g)
                for sid in hit_by_supplier[sup]:
                    services.setdefault(sid, f"supplier {g['suppliers'][sup].name}" if sup in g["suppliers"]
                                        else "supplier")
            for o in objectives:
                if risk.id in out[o.id]:
                    continue
                via = next((how for sid, how in services.items() if sid in o.service_ids), None)
                if via:
                    out[o.id][risk.id] = via
        return out

    def objective_specs(self, risk_ids=None) -> List[Dict[str, Any]]:
        """What the engines need: each objective with the ids of the risks that threaten it."""
        keep = set(risk_ids) if risk_ids is not None else None
        threats = self._threats()
        return [{"id": o.id, "name": o.name, "criticality": o.criticality,
                 "risk_ids": sorted(r for r in threats[o.id] if keep is None or r in keep),
                 "tolerance_loss": o.tolerance_loss, "tolerance_probability": o.tolerance_probability}
                for o in sorted(self.repo.list(Objective), key=lambda o: o.id)]

    # -- the view -------------------------------------------------------------------------------

    def objective_overview(self, residual: bool = True) -> Dict[str, Any]:
        """
        Every objective with its threats and the risk to it (with controls by default), plus
        what a review would ask: mission-critical objectives without mapped risks, owner,
        KPI or tolerance, and risks that threaten no objective. Monte Carlo when correlations
        are set (so they count), otherwise the exact analytic engine.
        """
        g = self._graph()
        threats = self._threats(g)
        risks = {r.id: r for r in g["risks"]}
        objectives = self.list_objectives()
        figures: Dict[str, Dict[str, Any]] = {}
        engine = None
        if objectives and risks:
            engine = "monte_carlo" if self.correlation_pairs() else "analytic"
            report = self.simulation_report(CHECK_TRIALS, 42, residual=residual, engine=engine)
            figures = {x["id"]: x for x in report.get("objectives", [])}
        findings: List[Dict[str, Any]] = []
        rows = []
        for o in objectives:
            t = threats[o.id]
            rows.append({**o.to_dict(), "threats": [{"id": rid, "name": risks[rid].name, "via": how}
                                                    for rid, how in sorted(t.items(), key=lambda x: risks[x[0]].name)],
                         "figures": figures.get(o.id)})
            critical = o.criticality == "MISSION_CRITICAL"
            if not t:
                findings.append({"severity": "high" if critical else "medium", "objective": o.name,
                                 "finding": "No risks mapped: is it really risk-free?"})
            if critical and not o.owner_id:
                findings.append({"severity": "medium", "objective": o.name, "finding": "No owner"})
            if critical and o.tolerance_loss is None:
                findings.append({"severity": "medium", "objective": o.name,
                                 "finding": "No tolerance: the analysis cannot say whether the risk is acceptable"})
            if critical and not o.kpi:
                findings.append({"severity": "low", "objective": o.name, "finding": "No KPI"})
            f = figures.get(o.id)
            if f and f["tolerance"] and f["tolerance"]["breached"]:
                findings.append({"severity": "high", "objective": o.name,
                                 "finding": f"Outside tolerance: {f['tolerance']['actual']:.1%} chance of losing more "
                                            f"than {f['tolerance']['loss']:,.0f} (accepted "
                                            f"{f['tolerance']['probability']:.1%})"})
        mapped = {rid for t in threats.values() for rid in t}
        unmapped = sorted((r for r in risks.values() if r.id not in mapped), key=lambda r: r.name)
        rank = {"high": 0, "medium": 1, "low": 2}
        return {"objectives": rows, "engine": engine, "residual": residual,
                "unmapped_risks": [{"id": r.id, "name": r.name} for r in unmapped] if objectives else [],
                "findings": sorted(findings, key=lambda f: rank[f["severity"]])}


OBJECTIVE_WRITE_METHODS = ("create_objective", "update_objective", "delete_objective")
