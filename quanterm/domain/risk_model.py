"""
Quantitative risk model for quantyERM.

A risk is described by two distributions:

* frequency - how many loss events happen per year
    {"type": "poisson",   "rate": 0.5}   # on average 0.5 events/year
    {"type": "bernoulli", "p": 0.1}      # at most one event, 10% chance/year

* impact - the loss of a single event (in money)
    {"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 1_000_000}
    {"dist": "pert", "min": 10_000, "mode": 50_000, "max": 400_000}
    {"dist": "triangular", "min": ..., "mode": ..., "max": ...}
    {"dist": "uniform", "min": ..., "max": ...}
    {"dist": "fixed", "value": 250_000}
    {"dist": "<any scipy.stats name>", "params": {...}}   # e.g. "gamma"

Annual loss for one risk = sum of `impact` over `frequency` events.
Risks are simulated independently with Monte Carlo and can be rolled up
by business unit (including child units) and for the whole organisation.
"""

import hashlib
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
from scipy import stats

Z90 = stats.norm.ppf(0.9)  # ~1.2816
DEFAULT_PERCENTILES = (50, 90, 95, 99)


# =========================================================================
# Distribution helpers
# =========================================================================

def _require(spec: Dict[str, Any], *keys: str):
    missing = [k for k in keys if k not in spec]
    if missing:
        raise ValueError(f"Distribution spec {spec} is missing: {', '.join(missing)}")


def build_impact_distribution(spec: Dict[str, Any]):
    """
    Turns an impact spec into a frozen scipy distribution
    (or None for a fixed value, handled by `sample_impact`).
    """
    dist = spec.get("dist")
    if dist == "lognorm_p10_p90":
        _require(spec, "p10", "p90")
        p10, p90 = float(spec["p10"]), float(spec["p90"])
        if not 0 < p10 < p90:
            raise ValueError("lognorm_p10_p90 requires 0 < p10 < p90")
        mu = (np.log(p10) + np.log(p90)) / 2
        sigma = (np.log(p90) - np.log(p10)) / (2 * Z90)
        return stats.lognorm(s=sigma, scale=np.exp(mu))
    if dist in ("pert", "triangular"):
        _require(spec, "min", "mode", "max")
        lo, mode, hi = float(spec["min"]), float(spec["mode"]), float(spec["max"])
        if not lo <= mode <= hi or lo == hi:
            raise ValueError(f"{dist} requires min <= mode <= max and min < max")
        width = hi - lo
        if dist == "triangular":
            return stats.triang(c=(mode - lo) / width, loc=lo, scale=width)
        lam = float(spec.get("lambda", 4.0))  # classic PERT uses 4
        a = 1 + lam * (mode - lo) / width
        b = 1 + lam * (hi - mode) / width
        return stats.beta(a, b, loc=lo, scale=width)
    if dist == "uniform":
        _require(spec, "min", "max")
        lo, hi = float(spec["min"]), float(spec["max"])
        if not lo < hi:
            raise ValueError("uniform requires min < max")
        return stats.uniform(loc=lo, scale=hi - lo)
    if dist == "fixed":
        _require(spec, "value")
        return None
    if dist and hasattr(stats, dist):
        try:
            return getattr(stats, dist)(**spec.get("params", {}))
        except Exception as e:
            raise ValueError(f"Could not create scipy distribution '{dist}' with {spec.get('params')}: {e}")
    raise ValueError(f"Unknown impact distribution: {dist!r}")


def validate_frequency(spec: Dict[str, Any]):
    ftype = spec.get("type")
    if ftype == "poisson":
        _require(spec, "rate")
        if float(spec["rate"]) < 0:
            raise ValueError("Poisson rate must be >= 0")
    elif ftype == "bernoulli":
        _require(spec, "p")
        if not 0 <= float(spec["p"]) <= 1:
            raise ValueError("Bernoulli p must be between 0 and 1")
    else:
        raise ValueError(f"Unknown frequency type: {ftype!r}")


def sample_frequency(spec: Dict[str, Any], n: int, rng: np.random.Generator) -> np.ndarray:
    validate_frequency(spec)
    if spec["type"] == "poisson":
        return rng.poisson(float(spec["rate"]), size=n)
    return (rng.random(n) < float(spec["p"])).astype(np.int64)


def sample_impact(spec: Dict[str, Any], n: int, rng: np.random.Generator) -> np.ndarray:
    if n == 0:
        return np.empty(0)
    dist = build_impact_distribution(spec)
    if dist is None:
        return np.full(n, float(spec["value"]))
    return np.asarray(dist.rvs(size=n, random_state=rng), dtype=float)


def expected_annual_loss(frequency: Dict[str, Any], impact: Dict[str, Any]) -> float:
    """Analytical expected annual loss (E[N] * E[X])."""
    validate_frequency(frequency)
    en = float(frequency["rate"]) if frequency["type"] == "poisson" else float(frequency["p"])
    dist = build_impact_distribution(impact)
    ex = float(impact["value"]) if dist is None else float(dist.mean())
    return en * ex


def summarize_distribution(dist_type: str, **params) -> Dict[str, Any]:
    """
    Analytical summary (P10/P50/P90, mean, std, skew, kurtosis)
    of a named scipy.stats distribution. Values are plain floats
    so the result can be stored as JSON.
    """
    try:
        dist_class = getattr(stats, dist_type)
    except AttributeError:
        raise ValueError(f"Distribution '{dist_type}' is not found in scipy.stats")
    try:
        dist = dist_class(**params)
    except Exception as e:
        raise ValueError(f"Failed to instantiate '{dist_type}' with {params}: {e}")
    skew, kurt = dist.stats(moments="sk")
    return {
        "dist_type": dist_type,
        "params": params,
        "p10": float(dist.ppf(0.1)),
        "p50": float(dist.ppf(0.5)),
        "p90": float(dist.ppf(0.9)),
        "mean": float(dist.mean()),
        "std": float(dist.std()),
        "skew": float(skew),
        "kurtosis": float(kurt),
    }


# =========================================================================
# Monte Carlo simulation
# =========================================================================

def simulate_annual_loss(frequency: Dict[str, Any], impact: Dict[str, Any],
                         n_trials: int, rng: np.random.Generator) -> np.ndarray:
    """
    Simulates the annual loss of one risk. Returns an array of length
    n_trials, one total loss per simulated year.
    """
    counts = sample_frequency(frequency, n_trials, rng)
    impacts = sample_impact(impact, int(counts.sum()), rng)
    trial_index = np.repeat(np.arange(n_trials), counts)
    return np.bincount(trial_index, weights=impacts, minlength=n_trials)


def loss_statistics(losses: np.ndarray,
                    percentiles: Iterable[float] = DEFAULT_PERCENTILES,
                    tail_level: float = 99) -> Dict[str, float]:
    """
    Key figures for a simulated annual-loss sample:
    mean (expected annual loss), std, chosen percentiles (VaR),
    TVaR / expected shortfall at `tail_level`, and P(loss > 0).
    """
    losses = np.asarray(losses, dtype=float)
    result = {
        "mean": float(losses.mean()),
        "std": float(losses.std()),
        "max": float(losses.max()),
        "prob_any_loss": float((losses > 0).mean()),
    }
    for p in percentiles:
        result[f"p{p:g}"] = float(np.percentile(losses, p))
    var = np.percentile(losses, tail_level)
    tail = losses[losses >= var]
    result[f"tvar{tail_level:g}"] = float(tail.mean()) if tail.size else float(var)
    return result


def loss_exceedance_curve(losses: np.ndarray, n_points: int = 20) -> List[Dict[str, float]]:
    """
    Points on the loss exceedance curve: for each loss threshold,
    the probability that annual loss exceeds it.
    """
    losses = np.asarray(losses, dtype=float)
    positive = losses[losses > 0]
    if positive.size == 0:
        return []
    thresholds = np.quantile(positive, np.linspace(0, 0.999, n_points))
    return [{"loss": float(t), "prob_exceed": float((losses > t).mean())}
            for t in np.unique(thresholds)]


class SimulationResult:
    """
    Holds simulated annual losses per risk and offers roll-ups.
    """
    def __init__(self, n_trials: int, seed: Optional[int],
                 risk_losses: Dict[str, np.ndarray], risks: Dict[str, Dict[str, Any]],
                 business_units: Dict[str, Dict[str, Any]]):
        self.n_trials = n_trials
        self.seed = seed
        self.risk_losses = risk_losses
        self.risks = risks
        self.business_units = business_units

    def _descendants(self, unit_id: str) -> List[str]:
        """unit_id plus all of its (grand)children."""
        children: Dict[str, List[str]] = {}
        for bu_id, bu in self.business_units.items():
            children.setdefault(bu.get("parent_id"), []).append(bu_id)
        out, stack = [], [unit_id]
        while stack:
            current = stack.pop()
            if current in out:
                continue  # guard against cycles
            out.append(current)
            stack.extend(children.get(current, []))
        return out

    def unit_losses(self, unit_id: str, include_children: bool = True) -> np.ndarray:
        units = set(self._descendants(unit_id) if include_children else [unit_id])
        total = np.zeros(self.n_trials)
        for risk_id, losses in self.risk_losses.items():
            if self.risks[risk_id].get("business_unit_id") in units:
                total += losses
        return total

    def total_losses(self) -> np.ndarray:
        total = np.zeros(self.n_trials)
        for losses in self.risk_losses.values():
            total += losses
        return total

    def report(self, percentiles: Iterable[float] = DEFAULT_PERCENTILES) -> Dict[str, Any]:
        """JSON-serialisable summary of the whole simulation."""
        percentiles = tuple(percentiles)
        per_risk = []
        for risk_id, losses in self.risk_losses.items():
            risk = self.risks[risk_id]
            per_risk.append({
                "id": risk_id,
                "name": risk["name"],
                "business_unit_id": risk.get("business_unit_id"),
                **loss_statistics(losses, percentiles),
            })
        per_risk.sort(key=lambda r: r["mean"], reverse=True)

        per_unit = []
        for bu_id, bu in self.business_units.items():
            own = self.unit_losses(bu_id, include_children=False)
            rolled = self.unit_losses(bu_id, include_children=True)
            per_unit.append({
                "id": bu_id,
                "name": bu["name"],
                "parent_id": bu.get("parent_id"),
                "own": loss_statistics(own, percentiles),
                "rolled_up": loss_statistics(rolled, percentiles),
            })

        total = self.total_losses()
        # Diversification: sum of stand-alone P99s vs. P99 of the total
        standalone_p99 = sum(float(np.percentile(l, 99)) for l in self.risk_losses.values())
        total_stats = loss_statistics(total, percentiles)
        return {
            "n_trials": self.n_trials,
            "seed": self.seed,
            "total": total_stats,
            "diversification_benefit_p99": standalone_p99 - total_stats["p99"],
            "loss_exceedance_curve": loss_exceedance_curve(total),
            "risks": per_risk,
            "business_units": per_unit,
        }


def _stable_key(text: str) -> int:
    """Deterministic 32-bit integer from a string (unlike hash())."""
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")


def run_simulation(risks: Dict[str, Dict[str, Any]],
                   business_units: Dict[str, Dict[str, Any]],
                   n_trials: int = 10_000, seed: Optional[int] = None,
                   residual: bool = True) -> SimulationResult:
    """
    Simulates every risk independently.

    With residual=True a risk's `residual_frequency` / `residual_impact`
    (after controls) is used when present, otherwise the inherent
    `frequency` / `impact`.
    """
    if n_trials <= 0:
        raise ValueError("n_trials must be positive")
    shared_rng = np.random.default_rng(seed)
    losses = {}
    for risk_id, risk in risks.items():
        freq = (residual and risk.get("residual_frequency")) or risk["frequency"]
        imp = (residual and risk.get("residual_impact")) or risk["impact"]
        # With a seed, each risk gets its own stream derived from its ID, so
        # adding/changing one risk (or switching inherent/residual) does not
        # change the random numbers drawn for the others.
        rng = (np.random.default_rng([seed, _stable_key(risk_id)])
               if seed is not None else shared_rng)
        losses[risk_id] = simulate_annual_loss(freq, imp, n_trials, rng)
    return SimulationResult(n_trials, seed, losses, risks, business_units)
