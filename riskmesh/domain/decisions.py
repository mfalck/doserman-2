"""
What a control is worth: its effect on the risks it mitigates, against its cost.

A control's effect is two shares: how much it reduces how often events
happen (`frequency_reduction`) and how much it reduces the loss of one event
(`impact_reduction`, which also shortens outages by the same share). Effects
of several controls on one risk multiply: two controls that each cut the
frequency by 50% leave 25%.

The effect works on the risk's current estimate (residual if it has one,
otherwise inherent):

- a planned control is valued by adding its effect: what it would save;
- an implemented control by taking its effect out: what we would lose
  without it (its residual estimate already includes it).
"""

import copy
from typing import Any, Dict, Optional

FREQ_KEYS = {"poisson": "rate", "bernoulli": "p", "negbin": "rate"}
SCALE_KEYS = {"lognorm_p10_p90": ("p10", "p90"), "pert": ("min", "mode", "max"),
              "triangular": ("min", "mode", "max"), "uniform": ("min", "max"), "fixed": ("value",)}


def scale_frequency(spec: Dict[str, Any], k: float) -> Dict[str, Any]:
    key = FREQ_KEYS[spec["type"]]
    value = float(spec[key]) * k
    if spec["type"] == "bernoulli":
        value = min(value, 1.0)
    out = {**spec, key: round(value, 8)}
    if spec["type"] == "negbin" and spec.get("rate_p90") is not None:
        out["rate_p90"] = round(float(spec["rate_p90"]) * k, 8)  # same shape: the whole rate scales
    return out


def scale_impact(spec: Dict[str, Any], k: float) -> Optional[Dict[str, Any]]:
    """The loss spec times k, or None for a distribution that cannot be scaled this way."""
    keys = SCALE_KEYS.get(spec.get("dist"))
    if not keys:
        return None
    return {**spec, **{key: round(float(spec[key]) * k, 4) for key in keys}}


def apply_effect(risk: Dict[str, Any], frequency_reduction: float, impact_reduction: float,
                 remove: bool = False) -> Dict[str, Any]:
    """
    A copy of an engine risk dict whose current (residual) estimate has the effect added,
    or taken out with remove=True. The result always has residual fields.
    """
    r = copy.deepcopy(risk)
    kf = 1.0 - (frequency_reduction or 0.0)
    ki = 1.0 - (impact_reduction or 0.0)
    if remove:
        kf, ki = 1.0 / kf, 1.0 / ki
    freq = r.get("residual_frequency") or r["frequency"]
    impact = r.get("residual_impact") or r["impact"]
    r["residual_frequency"] = scale_frequency(freq, kf) if kf != 1.0 else freq
    scaled = scale_impact(impact, ki) if ki != 1.0 else impact
    r["residual_impact"] = scaled or impact
    outage = r.get("residual_outage_hours") or r.get("outage_hours")
    if outage and ki != 1.0:
        r["residual_outage_hours"] = scale_impact(outage, ki) or outage
    return r


def annualised_cost(implementation_cost: Optional[float], annual_cost: Optional[float],
                    lifetime_years: float) -> float:
    return (annual_cost or 0.0) + (implementation_cost or 0.0) / max(lifetime_years or 1.0, 1e-9)
