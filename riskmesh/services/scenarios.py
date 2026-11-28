"""
Scenarios: named "what if the world turns" cases, compared with today.

A scenario sets drivers to other levels, hits individual risks harder
(frequency and/or loss multipliers on their current estimates) and can add an
outage of a supplier or service for a number of hours. The outage is costed
like the stress test: our own cost and the customers' penalties for that one
event, added to the scenario year. The result is today vs the scenario on the
total (expected loss, P90, P99), per risk, the appetite and the objectives.
Analytic engine: the levels and factors, not the drivers' year-to-year
variation.
"""

from typing import Any, Dict, List, Optional

from ..domain import analytic
from ..domain import decisions as dec
from ..domain.errors import ValidationError
from ..domain.models import BusinessUnit, Driver, Risk, Scenario


class Scenarios:
    """Mixin for RiskService (uses Drivers._state_at/_eal and ThirdParty.stress_test)."""

    def _validate_scenario(self, e) -> bool:
        if not isinstance(e, Scenario):
            return False
        self._name(e, "Scenario")
        levels = {}
        for did, lvl in (e.driver_levels or {}).items():
            if isinstance(lvl, bool) or not isinstance(lvl, (int, float)) or not 0.05 <= lvl <= 20:
                raise ValidationError("Driver levels must be numbers from 0.05 to 20.")
            levels[str(did)] = float(lvl)
        e.driver_levels = levels
        factors = {}
        for rid, f in (e.risk_factors or {}).items():
            if not isinstance(f, dict):
                raise ValidationError("Each risk factor needs a frequency and/or impact multiplier.")
            clean = {}
            for key in ("frequency", "impact"):
                v = f.get(key)
                if v is None:
                    continue
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 100:
                    raise ValidationError("Risk multipliers must be numbers above 0 and at most 100.")
                if v != 1:
                    clean[key] = float(v)
            if clean:
                factors[str(rid)] = clean
        e.risk_factors = factors
        if e.outage_supplier_id and e.outage_service_id:
            raise ValidationError("An outage is of a supplier or of a service, not both.")
        if (e.outage_supplier_id or e.outage_service_id) and not (e.outage_hours and e.outage_hours > 0):
            raise ValidationError("Give the outage's length in hours.")
        self._non_negative(e.outage_hours, "outage_hours")
        return True

    # -- CRUD ---------------------------------------------------------------------------------

    def create_scenario(self, name: str, **optional: Any) -> Scenario:
        return self._create(Scenario, name=name, **optional)

    def get_scenario(self, scenario_id: str) -> Scenario:
        return self._require(Scenario, scenario_id, "Scenario")

    def list_scenarios(self) -> List[Scenario]:
        return sorted(self.repo.list(Scenario), key=lambda s: s.name)

    def update_scenario(self, scenario_id: str, **changes: Any) -> Scenario:
        return self._update(Scenario, scenario_id, changes)

    def delete_scenario(self, scenario_id: str) -> None:
        self._delete(Scenario, scenario_id)

    # -- evaluation ---------------------------------------------------------------------------

    def _summary(self, state: Dict[str, Dict[str, Any]], units, extra: float = 0.0) -> Dict[str, Any]:
        """Total, appetite and objectives for a state - only the distributions those need."""
        specs = {rid: analytic._specs(r, True) for rid, r in state.items()}
        cache: Dict[frozenset, Any] = {}

        def dist(ids):
            key = frozenset(i for i in ids if i in specs)
            if key not in cache:
                cache[key] = analytic.aggregate([specs[i] for i in sorted(key)], analytic.DEFAULT_POINTS)
            return cache[key]

        total = dist(specs)
        appetite = []
        for a in self.appetites_for(None):
            ids = specs.keys()
            if a.get("business_unit_id"):
                below = set(self._descendant_ids(a["business_unit_id"]))
                ids = [rid for rid, r in state.items() if r.get("business_unit_id") in below]
            d = dist(ids)
            # the outage is a group-level loss: it counts against the organisation-wide appetite only
            shift = extra if not a.get("business_unit_id") else 0.0
            appetite.append({"name": a["name"], "breached": any(
                d.prob_exceed(max(p["loss"] - shift, 0.0)) > p["probability"] for p in a["points"])})
        objectives = []
        for o in self.objective_specs():
            d = dist(o["risk_ids"])
            breached = bool(o.get("tolerance_loss") is not None and
                            d.prob_exceed(o["tolerance_loss"]) > o["tolerance_probability"])
            objectives.append({"name": o["name"], "mean": d.statistics()["mean"], "breached": breached})
        st = total.statistics()
        return {"mean": st["mean"] + extra, "p90": st["p90"] + extra, "p99": st["p99"] + extra,
                "appetite": appetite, "objectives": objectives}

    def evaluate_scenario(self, scenario_id: Optional[str] = None, scenario: Optional[Scenario] = None,
                          _today: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Today vs the scenario (pass an unsaved Scenario to try one out)."""
        sc = scenario or self.get_scenario(scenario_id)
        if scenario is not None:
            self._validate_scenario(sc)
        drivers = {d.id: d for d in self.repo.list(Driver)}
        risks = {r.id: r for r in self.repo.list(Risk)}
        missing = [f"driver {d}" for d in sc.driver_levels if d not in drivers] + \
                  [f"risk {r}" for r in sc.risk_factors if r not in risks]
        levels = {d: lvl for d, lvl in sc.driver_levels.items() if d in drivers}
        units = {u.id: u.to_dict() for u in self.repo.list(BusinessUnit)}
        if _today is None:  # the overview computes today once for all scenarios
            state0 = self._state_at()
            _today = {"state": state0, "summary": self._summary(state0, units)}
        today = _today["state"]
        state = self._state_at(levels) if levels else {k: dict(v) for k, v in today.items()}
        for rid, f in sc.risk_factors.items():
            if rid not in state:
                continue
            r = dict(state[rid])
            freq = r.get("residual_frequency") or r["frequency"]
            impact = r.get("residual_impact") or r["impact"]
            r["residual_frequency"] = dec.scale_frequency(freq, f.get("frequency", 1.0))
            r["residual_impact"] = dec.scale_impact(impact, f.get("impact", 1.0)) or impact
            state[rid] = r
        outage = None
        if sc.outage_hours and (sc.outage_supplier_id or sc.outage_service_id):
            try:
                st = self.stress_test(sc.outage_hours, supplier_id=sc.outage_supplier_id,
                                      service_id=sc.outage_service_id)
                outage = {"source": st["source"]["name"], "hours": sc.outage_hours, "cost": st["total"],
                          "own_cost": st["own_cost"], "penalties": st["penalties"],
                          "notification_deadlines_missed": st["notification_deadlines_missed"]}
            except Exception:  # the supplier/service was deleted
                missing.append("outage source")
        before = _today["summary"]
        after = self._summary(state, units, outage["cost"] if outage else 0.0)
        per_risk = sorted(({"id": rid, "name": today[rid]["name"], "before": self._eal(today[rid]),
                            "after": self._eal(state[rid])} for rid in today
                           if abs(self._eal(today[rid]) - self._eal(state[rid])) > 1e-6),
                          key=lambda x: x["before"] - x["after"])
        return {
            "scenario": sc.to_dict(),
            "drivers": [{"id": d, "name": drivers[d].name, "from": drivers[d].level, "to": lvl}
                        for d, lvl in levels.items()],
            "risks": per_risk, "outage": outage, "before": before, "after": after,
            "missing": missing,
            "note": "Analytic engine: levels and multipliers; the outage is one event, added to the scenario year.",
        }

    def scenario_overview(self) -> Dict[str, Any]:
        """Every scenario next to today: total, P99 and how many appetites and objectives it breaks."""
        rows = []
        today = None
        scenarios = self.list_scenarios()
        cache = None
        if scenarios:
            units = {u.id: u.to_dict() for u in self.repo.list(BusinessUnit)}
            state0 = self._state_at()
            cache = {"state": state0, "summary": self._summary(state0, units)}
        for sc in scenarios:
            r = self.evaluate_scenario(sc.id, _today=cache)
            today = today or r["before"]
            a = r["after"]
            rows.append({"id": sc.id, "name": sc.name, "description": sc.description, "likelihood": sc.likelihood,
                         "mean": a["mean"], "p90": a["p90"], "p99": a["p99"],
                         "outage_cost": r["outage"]["cost"] if r["outage"] else 0.0,
                         "objectives_breached": [o["name"] for o in a["objectives"] if o["breached"]],
                         "appetite_breached": [x["name"] for x in a["appetite"] if x["breached"]],
                         # compared with today: what the scenario breaks, and what it brings back within tolerance
                         "newly_breached": [x["name"] for x, b in zip(a["objectives"] + a["appetite"],
                                                                      r["before"]["objectives"] + r["before"]["appetite"])
                                            if x["breached"] and not b["breached"]],
                         "resolved": [x["name"] for x, b in zip(a["objectives"] + a["appetite"],
                                                                r["before"]["objectives"] + r["before"]["appetite"])
                                      if b["breached"] and not x["breached"]],
                         "missing": r["missing"]})
        rows.sort(key=lambda x: x["p99"], reverse=True)
        return {"today": today, "scenarios": rows}


SCENARIO_WRITE_METHODS = ("create_scenario", "update_scenario", "delete_scenario")
