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

from typing import Any, Dict, List, Optional

from ..domain import analytic, risk_model
from ..domain import decisions as dec
from ..domain.consequences import Severity
from ..domain.errors import ValidationError
from ..domain.models import BusinessUnit, Control, Driver, Risk

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
        v = c.driver_reduction
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 0.9):
            raise ValidationError("driver_reduction must be a share from 0 to 0.9 (e.g. 0.3 lowers the level by 30%).")
        if v and not c.driver_id:
            raise ValidationError("Choose the driver the control improves.")
        if isinstance(c.lifetime_years, bool) or not isinstance(c.lifetime_years, (int, float)) or c.lifetime_years <= 0:
            raise ValidationError("lifetime_years must be positive.")

    @staticmethod
    def _has_direct(c: Control) -> bool:
        return bool((c.frequency_reduction or c.impact_reduction) and c.risk_ids)

    @staticmethod
    def _driver_part(c: Control, drivers: Dict[str, Driver]):
        """(driver id, level multiplier) when the control improves a driver."""
        if c.driver_id and c.driver_reduction and c.driver_id in drivers:
            return c.driver_id, 1.0 - c.driver_reduction
        return None

    def _has_effect(self, c: Control, drivers: Optional[Dict[str, Driver]] = None) -> bool:
        drivers = drivers if drivers is not None else {d.id: d for d in self.repo.list(Driver)}
        return self._has_direct(c) or self._driver_part(c, drivers) is not None

    def _ctx_state(self, levels: Dict[str, float], added: List[Control]) -> Dict[str, Dict[str, Any]]:
        """The risks with these driver levels and the direct effects of the added controls."""
        state = self._state_at(levels)
        for c in added:
            if self._has_direct(c):
                for rid in c.risk_ids:
                    if rid in state:
                        state[rid] = dec.apply_effect(state[rid], c.frequency_reduction or 0, c.impact_reduction or 0)
        return state

    def _new_ctx(self) -> Dict[str, Any]:
        drivers = {d.id: d for d in self.repo.list(Driver)}
        return {"levels": {}, "added": [], "drivers": drivers, "state": self._ctx_state({}, [])}

    def _value(self, c: Control, ctx: Dict[str, Any], tail: bool = True, base_p99: Any = None) -> Dict[str, Any]:
        """
        The control's value on the given state: adds a planned control, removes an implemented one.
        Its direct effect works on its own risks; its driver effect on every risk the driver drives.
        """
        implemented = c.implementation_status == "IMPLEMENTED"
        state = ctx["state"]
        dp = self._driver_part(c, ctx["drivers"])
        if dp:
            did, k = dp
            levels = dict(ctx["levels"])
            current = levels.get(did, ctx["drivers"][did].level)
            levels[did] = min(max(current / k if implemented else current * k, 0.05), 20.0)
            other = self._ctx_state(levels, ctx["added"])
        else:
            other = dict(state)
        if self._has_direct(c):
            for rid in c.risk_ids:
                if rid in other:
                    other[rid] = dec.apply_effect(other[rid], c.frequency_reduction or 0, c.impact_reduction or 0,
                                                  remove=implemented)
        without, with_ = (other, state) if implemented else (state, other)
        ids = [rid for rid in state if rid in other and abs(_eal(without[rid]) - _eal(with_[rid])) > 1e-6]
        benefit = sum(_eal(without[rid]) - _eal(with_[rid]) for rid in ids)
        cost = dec.annualised_cost(c.implementation_cost, c.annual_cost, c.lifetime_years)
        out = {
            "id": c.id, "name": c.name, "status": c.implementation_status, "risk_count": len(ids),
            "risks": [{"id": rid, "name": state[rid]["name"], "eal_without": _eal(without[rid]),
                       "eal_with": _eal(with_[rid])} for rid in ids],
            "frequency_reduction": c.frequency_reduction, "impact_reduction": c.impact_reduction,
            "driver": ({"id": dp[0], "name": ctx["drivers"][dp[0]].name, "reduction": c.driver_reduction}
                       if dp else None),
            "implementation_cost": c.implementation_cost, "annual_cost": c.annual_cost,
            "lifetime_years": c.lifetime_years, "annualised_cost": cost,
            "benefit": benefit, "net_benefit": benefit - cost,
            "rosi": (benefit - cost) / cost if cost > 0 else None,
            "benefit_per_cost": benefit / cost if cost > 0 else None,
            "payback_years": (c.implementation_cost or 0) / (benefit - (c.annual_cost or 0))
            if benefit > (c.annual_cost or 0) and c.implementation_cost else None,
            "_other": other,
        }
        if tail and ids:
            # one side is the current state, whose P99 the caller computed once
            current = base_p99 if base_p99 is not None else _total_p99(state)
            p_other = _total_p99(other)
            out["p99_without"], out["p99_with"] = (p_other, current) if implemented else (current, p_other)
        return out

    def control_values(self, tail: bool = True) -> Dict[str, Any]:
        """Every active control with an effect, valued; and those not yet quantified."""
        ctx = self._new_ctx()
        risks = ctx["state"]
        drivers = ctx["drivers"]
        controls = [c for c in self.repo.list(Control) if c.implementation_status in ACTIVE]
        base = _total_p99(risks) if (tail and risks) else None
        valued = []
        for c in controls:
            if self._has_effect(c, drivers):
                v = self._value(c, ctx, tail, base)
                v.pop("_other")
                valued.append(v)
        valued.sort(key=lambda v: v["net_benefit"], reverse=True)

        def gaps(c: Control) -> List[str]:
            direct = bool(c.frequency_reduction or c.impact_reduction)
            via_driver = self._driver_part(c, drivers) is not None
            out = []
            if not (direct or via_driver):
                out.append("effect")
            if c.implementation_cost is None and c.annual_cost is None:
                out.append("cost")
            if direct and not c.risk_ids and not via_driver:
                out.append("risks")
            return out
        missing = sorted(({"id": c.id, "name": c.name, "status": c.implementation_status, "missing": gaps(c)}
                          for c in controls), key=lambda x: x["name"])
        return {"controls": valued, "not_quantified": [m for m in missing if m["missing"]],
                "total_p99": base if base is not None else (_total_p99(risks) if risks else 0.0)}

    def investment_plan(self, budget: float, include_in_progress: bool = True) -> Dict[str, Any]:
        """Which planned controls to fund within `budget` (first-year cost), and what that changes."""
        if budget is None or budget < 0:
            raise ValidationError("budget must be 0 or more.")
        statuses = CANDIDATES if include_in_progress else ("PLANNED",)
        ctx = self._new_ctx()
        before = ctx["state"]
        pool = [c for c in self.repo.list(Control)
                if c.implementation_status in statuses and self._has_effect(c, ctx["drivers"])]
        chosen, spent, steps = [], 0.0, []
        while True:
            best = None
            for c in pool:
                first_year = (c.implementation_cost or 0) + (c.annual_cost or 0)
                if spent + first_year > budget + 1e-9:
                    continue
                v = self._value(c, ctx, tail=False)
                if v["benefit"] <= 0:
                    continue
                score = v["benefit"] / v["annualised_cost"] if v["annualised_cost"] > 0 else float("inf")
                if best is None or score > best[0]:
                    best = (score, c, v, first_year)
            if best is None:
                break
            _, c, v, first_year = best
            dp = self._driver_part(c, ctx["drivers"])
            if dp:
                current = ctx["levels"].get(dp[0], ctx["drivers"][dp[0]].level)
                ctx["levels"][dp[0]] = min(max(current * dp[1], 0.05), 20.0)
            if self._has_direct(c):
                ctx["added"].append(c)
            ctx["state"] = v["_other"]
            pool.remove(c)
            spent += first_year
            chosen.append(c)
            steps.append({"id": c.id, "name": c.name, "first_year_cost": first_year,
                          "annualised_cost": v["annualised_cost"], "benefit": v["benefit"],
                          "net_benefit": v["net_benefit"], "benefit_per_cost": v["benefit_per_cost"],
                          "cumulative_cost": spent, "via_driver": v["driver"]["name"] if v["driver"] else None})
        state = ctx["state"]
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
        drivers = {d.id: d for d in self.repo.list(Driver)}
        dp = self._driver_part(c, drivers)
        basis = f"effect of control '{c.name}' built in"
        if self._has_direct(c):
            basis += (f": frequency -{(c.frequency_reduction or 0):.0%}, "
                      f"loss per event -{(c.impact_reduction or 0):.0%}")
        if dp:
            basis += f"; driver '{drivers[dp[0]].name}' -{c.driver_reduction:.0%}"
        reason = f"{self.reason} - {basis}" if self.reason else basis
        with self.context(reason=reason):
            if self._has_direct(c):
                for rid in c.risk_ids:
                    risk = self.repo.get(Risk, rid)
                    if risk is None:
                        continue
                    r = dec.apply_effect(risk.to_dict(), c.frequency_reduction or 0, c.impact_reduction or 0)
                    changes = {"residual_frequency": r["residual_frequency"], "residual_impact": r["residual_impact"]}
                    if r.get("residual_outage_hours"):
                        changes["residual_outage_hours"] = r["residual_outage_hours"]
                    self.update_risk(rid, **changes)
            if dp:  # the driver is the state of things today: the control moves it
                self.update_driver(dp[0], level=round(min(max(drivers[dp[0]].level * dp[1], 0.05), 20.0), 6))
            return self.update_control(control_id, implementation_status="IMPLEMENTED")


DECISION_WRITE_METHODS = ("implement_control",)
