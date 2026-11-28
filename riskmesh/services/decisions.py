"""
Decision insight: what each control is worth, and which planned controls to
fund within a budget.

Value of a control = the expected annual loss it removes from the risks it
mitigates (see domain/decisions.py for how its effect is applied), compared
with its annualised cost (annual cost + implementation cost spread over its
lifetime). Its effect on the tail is the change in P99 of the total.

The investment plan picks planned (and in-progress) controls greedily by
expected loss removed per unit of cost, each time on top of those already
picked, so two controls on the same risk are not counted twice; it stops at
the budget (cost in the first year: implementation plus annual cost). It then
shows the total, the appetite and the objectives before and after. The tail
figures come from the exact analytic engine, which treats risks as
independent.
"""

from typing import Any, Dict

from ..domain import analytic, decisions as dec, risk_model
from ..domain.consequences import Severity
from ..domain.errors import ValidationError
from ..domain.models import BusinessUnit, Control, Risk

ACTIVE = ("PLANNED", "IN_PROGRESS", "IMPLEMENTED")
CANDIDATES = ("PLANNED", "IN_PROGRESS")


def _eal(risk: Dict[str, Any]) -> float:
    freq = risk.get("residual_frequency") or risk["frequency"]
    return risk_model.expected_annual_loss(freq, Severity.from_risk(risk, True))


def _total_p99(risks: Dict[str, Dict[str, Any]]) -> float:
    dist = analytic.aggregate([analytic._specs(r, True) for r in risks.values()], analytic.DEFAULT_POINTS)
    return dist.quantile(0.99)


class Decisions:
    """Mixin for RiskService."""

    def _validate_control_economics(self, c: Control) -> None:
        for f in ("implementation_cost", "annual_cost"):
            self._non_negative(getattr(c, f), f)
        for f in ("frequency_reduction", "impact_reduction"):
            v = getattr(c, f)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 0.99):
                raise ValidationError(f"{f} must be a share from 0 to 0.99 (e.g. 0.3 for 30%).")
        if isinstance(c.lifetime_years, bool) or not isinstance(c.lifetime_years, (int, float)) or c.lifetime_years <= 0:
            raise ValidationError("lifetime_years must be positive.")

    @staticmethod
    def _has_effect(c: Control) -> bool:
        return bool(c.frequency_reduction or c.impact_reduction)

    def _decision_state(self) -> Dict[str, Dict[str, Any]]:
        return self.engine_risks(self.repo.list(Risk))

    def _value(self, c: Control, risks: Dict[str, Dict[str, Any]], tail: bool = True,
               base_p99: Any = None) -> Dict[str, Any]:
        """The control's value on the given state (adds a planned control, removes an implemented one)."""
        implemented = c.implementation_status == "IMPLEMENTED"
        ids = [rid for rid in c.risk_ids if rid in risks]
        changed = {rid: dec.apply_effect(risks[rid], c.frequency_reduction or 0, c.impact_reduction or 0,
                                         remove=implemented) for rid in ids}
        without = {rid: (changed[rid] if implemented else risks[rid]) for rid in ids}
        with_ = {rid: (risks[rid] if implemented else changed[rid]) for rid in ids}
        benefit = sum(_eal(without[rid]) - _eal(with_[rid]) for rid in ids)
        cost = dec.annualised_cost(c.implementation_cost, c.annual_cost, c.lifetime_years)
        out = {
            "id": c.id, "name": c.name, "status": c.implementation_status, "risk_count": len(ids),
            "risks": [{"id": rid, "name": risks[rid]["name"], "eal_without": _eal(without[rid]),
                       "eal_with": _eal(with_[rid])} for rid in ids],
            "frequency_reduction": c.frequency_reduction, "impact_reduction": c.impact_reduction,
            "implementation_cost": c.implementation_cost, "annual_cost": c.annual_cost,
            "lifetime_years": c.lifetime_years, "annualised_cost": cost,
            "benefit": benefit, "net_benefit": benefit - cost,
            "rosi": (benefit - cost) / cost if cost > 0 else None,
            "benefit_per_cost": benefit / cost if cost > 0 else None,
            "payback_years": (c.implementation_cost or 0) / (benefit - (c.annual_cost or 0))
            if benefit > (c.annual_cost or 0) and c.implementation_cost else None,
        }
        if tail and ids:
            # one side is the current state, whose P99 the caller computed once
            current = base_p99 if base_p99 is not None else _total_p99(risks)
            other = _total_p99({**risks, **(without if implemented else with_)})
            out["p99_without"], out["p99_with"] = (other, current) if implemented else (current, other)
        return out

    def control_values(self, tail: bool = True) -> Dict[str, Any]:
        """Every active control with an effect, valued; and those not yet quantified."""
        risks = self._decision_state()
        controls = [c for c in self.repo.list(Control) if c.implementation_status in ACTIVE]
        base = _total_p99(risks) if (tail and risks) else None
        valued = [self._value(c, risks, tail, base) for c in controls if self._has_effect(c) and c.risk_ids]
        valued.sort(key=lambda v: v["net_benefit"], reverse=True)
        missing = sorted(({"id": c.id, "name": c.name, "status": c.implementation_status,
                           "missing": [m for m, ok in (("effect", self._has_effect(c)),
                                                       ("cost", c.implementation_cost is not None
                                                        or c.annual_cost is not None),
                                                       ("risks", bool(c.risk_ids))) if not ok]}
                          for c in controls), key=lambda x: x["name"])
        return {"controls": valued, "not_quantified": [m for m in missing if m["missing"]],
                "total_p99": base if base is not None else (_total_p99(risks) if risks else 0.0)}

    def investment_plan(self, budget: float, include_in_progress: bool = True) -> Dict[str, Any]:
        """Which planned controls to fund within `budget` (first-year cost), and what that changes."""
        if budget is None or budget < 0:
            raise ValidationError("budget must be 0 or more.")
        statuses = CANDIDATES if include_in_progress else ("PLANNED",)
        state = self._decision_state()
        before = dict(state)
        pool = [c for c in self.repo.list(Control)
                if c.implementation_status in statuses and self._has_effect(c) and c.risk_ids]
        chosen, spent, steps = [], 0.0, []
        while True:
            best = None
            for c in pool:
                first_year = (c.implementation_cost or 0) + (c.annual_cost or 0)
                if spent + first_year > budget + 1e-9:
                    continue
                v = self._value(c, state, tail=False)
                if v["benefit"] <= 0:
                    continue
                score = v["benefit"] / v["annualised_cost"] if v["annualised_cost"] > 0 else float("inf")
                if best is None or score > best[0]:
                    best = (score, c, v, first_year)
            if best is None:
                break
            _, c, v, first_year = best
            for rid in c.risk_ids:
                if rid in state:
                    state[rid] = dec.apply_effect(state[rid], c.frequency_reduction or 0, c.impact_reduction or 0)
            pool.remove(c)
            spent += first_year
            chosen.append(c)
            steps.append({"id": c.id, "name": c.name, "first_year_cost": first_year,
                          "annualised_cost": v["annualised_cost"], "benefit": v["benefit"],
                          "net_benefit": v["net_benefit"], "benefit_per_cost": v["benefit_per_cost"],
                          "cumulative_cost": spent})
        left_out = [{"id": c.id, "name": c.name,
                     "first_year_cost": (c.implementation_cost or 0) + (c.annual_cost or 0)} for c in pool]
        units = {u.id: u.to_dict() for u in self.repo.list(BusinessUnit)}
        out = {"budget": budget, "spent": spent, "chosen": steps, "left_out": left_out,
               "note": "Tail figures from the analytic engine (risks independent)."}
        if before:
            reports = {}
            for key, risks in (("before", before), ("after", state)):
                reports[key] = analytic.analyse(risks, units, residual=True, appetites=self.appetites_for(None),
                                                objectives=self.objective_specs())
            for key, r in reports.items():
                out[key] = {"mean": r["total"]["mean"], "p90": r["total"]["p90"], "p99": r["total"]["p99"],
                            "appetite": [{"name": a["name"], "breached": a["breached"]} for a in r.get("appetite", [])],
                            "objectives": [{"name": o["name"], "breached": bool(o["tolerance"] and
                                                                                o["tolerance"]["breached"]),
                                            "mean": o["mean"]} for o in r.get("objectives", [])]}
            out["annual_benefit"] = out["before"]["mean"] - out["after"]["mean"]
        return out

    def implement_control(self, control_id: str) -> Control:
        """
        Marks a planned control implemented and builds its effect into the residual
        estimates of its risks, so it is not counted twice later (recorded in the audit log).
        """
        c = self._require(Control, control_id, "Control")
        if c.implementation_status not in CANDIDATES:
            raise ValidationError("Only a planned or in-progress control can be implemented.")
        if not self._has_effect(c):
            raise ValidationError("The control has no effect registered; set the status by hand instead.")
        basis = (f"effect of control '{c.name}' built in: frequency -{(c.frequency_reduction or 0):.0%}, "
                 f"loss per event -{(c.impact_reduction or 0):.0%}")
        reason = f"{self.reason} - {basis}" if self.reason else basis
        with self.context(reason=reason):
            for rid in c.risk_ids:
                risk = self.repo.get(Risk, rid)
                if risk is None:
                    continue
                r = dec.apply_effect(risk.to_dict(), c.frequency_reduction or 0, c.impact_reduction or 0)
                changes = {"residual_frequency": r["residual_frequency"], "residual_impact": r["residual_impact"]}
                if r.get("residual_outage_hours"):
                    changes["residual_outage_hours"] = r["residual_outage_hours"]
                self.update_risk(rid, **changes)
            return self.update_control(control_id, implementation_status="IMPLEMENTED")


DECISION_WRITE_METHODS = ("implement_control",)
