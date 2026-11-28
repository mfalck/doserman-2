"""
Drivers: common causes behind several risks, and what changing them would do.

A driver's level scales the risks it drives through their sensitivities
(elasticities): with level L and frequency sensitivity s, the frequency is
multiplied by L**s (likewise the loss per event). Level 1 means "as
estimated", so the estimates stay what they are until someone changes a
level - which is how an intervention is tried out ("if we fix patching, the
driver drops to 0.6: what happens to ransomware, the data breach and our
objectives?").

The year-to-year variation (`uncertainty`, the multiplier in a 1-in-10 bad
year) makes the driven risks have bad years together, for a reason; the Monte
Carlo engine applies it, the analytic engine only the level.
"""

import math
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..domain import analytic, risk_model
from ..domain import decisions as dec
from ..domain.consequences import Severity
from ..domain.errors import ConflictError, ValidationError
from ..domain.models import BusinessUnit, Driver, DriverEffect, Risk
from .core import ServiceCore

Z90 = 1.2815515655446004
SWING = 0.2   # the tornado moves each driver 20% down and up


def sigma_of(uncertainty: float) -> float:
    return math.log(max(float(uncertainty), 1.0)) / Z90


class Drivers(ServiceCore):
    """Mixin for RiskService."""

    if TYPE_CHECKING:  # members of other service areas this one uses
        def _graph(self) -> Dict[str, Any]: ...
        def appetites_for(self, unit_id: Optional[str] = None) -> List[Dict[str, Any]]: ...
        def engine_risk(self, risk: Risk, g: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...
        def objective_specs(self, risk_ids=None) -> List[Dict[str, Any]]: ...


    def _validate_driver(self, e) -> bool:
        if isinstance(e, Driver):
            self._name(e, "Driver")
            for f, lo, hi in (("level", 0.05, 20.0), ("uncertainty", 1.0, 10.0)):
                v = getattr(e, f)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                    raise ValidationError(f"{f} must be a number from {lo:g} to {hi:g}.")
            return True
        if isinstance(e, DriverEffect):
            for f in ("frequency_sensitivity", "impact_sensitivity"):
                v = getattr(e, f)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 3:
                    raise ValidationError(f"{f} must be a number from 0 to 3.")
            if not (e.frequency_sensitivity or e.impact_sensitivity):
                raise ValidationError("A driver effect needs a frequency or an impact sensitivity.")
            return True
        return False

    # -- CRUD ---------------------------------------------------------------------------------

    def create_driver(self, name: str, **optional: Any) -> Driver:
        return self._create(Driver, name=name, **optional)

    def get_driver(self, driver_id: str) -> Driver:
        return self._require(Driver, driver_id, "Driver")

    def list_drivers(self) -> List[Driver]:
        return sorted(self.repo.list(Driver), key=lambda d: d.name)

    def update_driver(self, driver_id: str, **changes: Any) -> Driver:
        return self._update(Driver, driver_id, changes)

    def delete_driver(self, driver_id: str) -> None:
        """Its effects on risks go with it."""
        self._delete(Driver, driver_id)

    def _check_effect_unique(self, e: DriverEffect) -> None:
        for other in self.repo.list(DriverEffect, driver_id=e.driver_id):
            if other.id != e.id and other.risk_id == e.risk_id:
                raise ConflictError("This driver already has an effect on that risk; change that one instead.")

    def create_driver_effect(self, driver_id: str, risk_id: str, **optional: Any) -> DriverEffect:
        self._check_effect_unique(DriverEffect(driver_id=driver_id, risk_id=risk_id))
        return self._create(DriverEffect, driver_id=driver_id, risk_id=risk_id, **optional)

    def list_driver_effects(self, driver_id: Optional[str] = None, risk_id: Optional[str] = None) -> List[DriverEffect]:
        out = self.repo.list(DriverEffect)
        return [e for e in out if (not driver_id or e.driver_id == driver_id) and (not risk_id or e.risk_id == risk_id)]

    def update_driver_effect(self, effect_id: str, **changes: Any) -> DriverEffect:
        current = self._require(DriverEffect, effect_id, "Driver effect")
        self._check_effect_unique(DriverEffect.from_dict({**current.to_dict(), **changes}))
        return self._update(DriverEffect, effect_id, changes)

    def delete_driver_effect(self, effect_id: str) -> None:
        self._delete(DriverEffect, effect_id)

    # -- what the engines see -------------------------------------------------------------------

    def _driver_links(self, levels: Optional[Dict[str, float]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """risk id -> [{"id", "level", "sigma", "f", "i"}]; `levels` overrides drivers' levels (what-if)."""
        drivers = {d.id: d for d in self.repo.list(Driver)}
        out: Dict[str, List[Dict[str, Any]]] = {}
        for e in sorted(self.repo.list(DriverEffect), key=lambda e: e.id):
            d = drivers.get(e.driver_id)
            if d is None:
                continue
            level = (levels or {}).get(d.id, d.level)
            out.setdefault(e.risk_id, []).append({"id": d.id, "level": float(level), "sigma": sigma_of(d.uncertainty),
                                                  "f": e.frequency_sensitivity, "i": e.impact_sensitivity})
        return out

    @staticmethod
    def _apply_driver_levels(d: Dict[str, Any], links: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Scales the risk's specs by its drivers' levels and attaches the links for the Monte Carlo engine."""
        kf = math.prod(link["level"] ** link["f"] for link in links)
        ki = math.prod(link["level"] ** link["i"] for link in links)
        if abs(kf - 1) > 1e-12:
            for f in ("frequency", "residual_frequency"):
                if d.get(f):
                    d[f] = dec.scale_frequency(d[f], kf)
        if abs(ki - 1) > 1e-12:
            for f in ("impact", "residual_impact"):
                if d.get(f):
                    d[f] = dec.scale_impact(d[f], ki) or d[f]
        d["drivers"] = [{"id": link["id"], "level": link["level"], "sigma": link["sigma"], "f": link["f"],
                         "i": link["i"]} for link in links]
        return d

    # -- analysis --------------------------------------------------------------------------------

    def _state_at(self, levels: Optional[Dict[str, float]] = None) -> Dict[str, Dict[str, Any]]:
        risks = self.repo.list(Risk)
        g = self._graph() if any(r.outage_hours or r.residual_outage_hours for r in risks) else {}
        g = {**g, "driver_links": self._driver_links(levels)}
        return {r.id: self.engine_risk(r, g) for r in risks}

    @staticmethod
    def _eal(r: Dict[str, Any]) -> float:
        freq = r.get("residual_frequency") or r["frequency"]
        return risk_model.expected_annual_loss(freq, Severity.from_risk(r, True))

    def _totals(self, state: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
        if not state:
            return {"mean": 0.0, "p90": 0.0, "p99": 0.0}
        dist = analytic.aggregate([analytic._specs(r, True) for r in state.values()], analytic.DEFAULT_POINTS)
        return {"mean": sum(self._eal(r) for r in state.values()), "p90": dist.quantile(0.9), "p99": dist.quantile(0.99)}

    def driver_overview(self) -> Dict[str, Any]:
        """
        Every driver with the risks it drives and a tornado: the total expected loss and P99
        with the driver 20% lower and 20% higher, the others unchanged. Analytic (levels only).
        """
        drivers = self.list_drivers()
        risks = {r.id: r for r in self.repo.list(Risk)}
        current = self._state_at()
        base = self._totals(current)
        rows = []
        for d in drivers:
            effects = [e for e in self.repo.list(DriverEffect, driver_id=d.id) if e.risk_id in risks]
            low = self._totals(self._state_at({d.id: d.level * (1 - SWING)}))
            high = self._totals(self._state_at({d.id: d.level * (1 + SWING)}))
            rows.append({**d.to_dict(),
                         "effects": [{"id": e.id, "risk_id": e.risk_id, "risk": risks[e.risk_id].name,
                                      "frequency_sensitivity": e.frequency_sensitivity,
                                      "impact_sensitivity": e.impact_sensitivity, "rationale": e.rationale,
                                      "eal": self._eal(current[e.risk_id])} for e in effects],
                         "driven_eal": sum(self._eal(current[e.risk_id]) for e in effects),
                         "low": low, "high": high, "swing_mean": high["mean"] - low["mean"],
                         "swing_p99": high["p99"] - low["p99"]})
        rows.sort(key=lambda r: r["swing_mean"], reverse=True)
        undriven = sorted(r.name for r in risks.values() if not current[r.id].get("drivers"))
        return {"drivers": rows, "base": base, "swing": SWING, "risks_without_driver": undriven,
                "note": "Analytic engine: levels only; the year-to-year variation is applied by Monte Carlo."}

    def driver_what_if(self, levels: Dict[str, float]) -> Dict[str, Any]:
        """The effect of setting drivers to these levels: per risk, the total, appetite and objectives."""
        known = {d.id: d for d in self.repo.list(Driver)}
        for did, lvl in levels.items():
            if did not in known:
                raise ValidationError(f"Unknown driver {did}.")
            if isinstance(lvl, bool) or not isinstance(lvl, (int, float)) or not 0.05 <= lvl <= 20:
                raise ValidationError("Levels must be numbers from 0.05 to 20.")
        before, after = self._state_at(), self._state_at(levels)
        units = {u.id: u.to_dict() for u in self.repo.list(BusinessUnit)}
        out: Dict[str, Any] = {"levels": {did: {"name": known[did].name, "from": known[did].level, "to": lvl}
                                          for did, lvl in levels.items()}}
        out["risks"] = sorted(({"id": rid, "name": before[rid]["name"], "before": self._eal(before[rid]),
                                "after": self._eal(after[rid])} for rid in before
                               if abs(self._eal(before[rid]) - self._eal(after[rid])) > 1e-6),
                              key=lambda r: r["after"] - r["before"])
        for key, state in (("before", before), ("after", after)):
            if not state:
                continue
            r = analytic.analyse(state, units, residual=True, appetites=self.appetites_for(None),
                                 objectives=self.objective_specs())
            out[key] = {"mean": r["total"]["mean"], "p90": r["total"]["p90"], "p99": r["total"]["p99"],
                        "appetite": [{"name": a["name"], "breached": a["breached"]} for a in r.get("appetite", [])],
                        "objectives": [{"name": o["name"], "mean": o["mean"],
                                        "breached": bool(o["tolerance"] and o["tolerance"]["breached"])}
                                       for o in r.get("objectives", [])]}
        return out


DRIVER_WRITE_METHODS = ("create_driver", "update_driver", "delete_driver", "create_driver_effect",
                        "update_driver_effect", "delete_driver_effect")
