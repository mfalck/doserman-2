"""
Quantitative risk model for RiskMesh.

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
An event can also take services down for a while and so cost us and our
customers at the same time (a common shock): see consequences.py.
"""

import hashlib
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
from scipy import stats

Z90 = stats.norm.ppf(0.9)  # ~1.2816
DEFAULT_PERCENTILES = (50, 90, 95, 99)
ENGINE_VERSION = "monte_carlo-1"  # bump when results change, so stored runs are not reused


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
            raise ValueError(f"Could not create scipy distribution '{dist}' with {spec.get('params')}: {e}") from e
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


def expected_annual_loss(frequency: Dict[str, Any], impact) -> float:
    """Analytical expected annual loss (E[N] * E[X]). `impact` is a spec or a consequences.Severity."""
    validate_frequency(frequency)
    en = float(frequency["rate"]) if frequency["type"] == "poisson" else float(frequency["p"])
    if hasattr(impact, "mean"):
        return en * impact.mean()
    dist = build_impact_distribution(impact)
    ex = float(impact["value"]) if dist is None else float(dist.mean())
    return en * ex


# =========================================================================
# Qualitative scores (1-5) derived from the quantitative specs
# =========================================================================

# Likelihood: probability of at least one event per year
DEFAULT_LIKELIHOOD_BANDS = (0.05, 0.20, 0.50, 0.80)
# Impact: median loss of a single event (currency units)
DEFAULT_IMPACT_BANDS = (50_000, 250_000, 1_000_000, 5_000_000)
SEVERITY_LEVELS = ("Very Low", "Low", "Medium", "High", "Very High")


def annual_event_probability(frequency: Dict[str, Any]) -> float:
    """P(at least one event in a year)."""
    validate_frequency(frequency)
    if frequency["type"] == "poisson":
        return float(1 - np.exp(-float(frequency["rate"])))
    return float(frequency["p"])


def typical_event_loss(impact) -> float:
    """Median loss of one event. `impact` is a spec or a consequences.Severity."""
    if hasattr(impact, "median"):
        return impact.median()
    dist = build_impact_distribution(impact)
    return float(impact["value"]) if dist is None else float(dist.median())


def to_band(value: float, bands) -> int:
    """1 + number of thresholds reached -> 1..len(bands)+1."""
    return 1 + sum(1 for b in bands if value >= b)


def severity_level(score: int) -> str:
    """Score = likelihood x impact (1..25) -> label, same cut-offs as a 5x5 heat map."""
    if score >= 16:
        return "Very High"
    if score >= 9:
        return "High"
    if score >= 4:
        return "Medium"
    if score >= 2:
        return "Low"
    return "Very Low"


def risk_scores(frequency: Dict[str, Any], impact,
                likelihood_bands=DEFAULT_LIKELIHOOD_BANDS,
                impact_bands=DEFAULT_IMPACT_BANDS) -> Dict[str, Any]:
    likelihood = to_band(annual_event_probability(frequency), likelihood_bands)
    impact_score = to_band(typical_event_loss(impact), impact_bands)
    score = likelihood * impact_score
    return {
        "likelihood": likelihood,
        "impact": impact_score,
        "score": score,
        "level": severity_level(score),
        "annual_probability": annual_event_probability(frequency),
        "typical_event_loss": typical_event_loss(impact),
        "expected_annual_loss": expected_annual_loss(frequency, impact),
    }


def summarize_distribution(dist_type: str, **params) -> Dict[str, Any]:
    """
    Analytical summary (P10/P50/P90, mean, std, skew, kurtosis)
    of a named scipy.stats distribution. Values are plain floats
    so the result can be stored as JSON.
    """
    try:
        dist_class = getattr(stats, dist_type)
    except AttributeError:
        raise ValueError(f"Distribution '{dist_type}' is not found in scipy.stats") from None
    try:
        dist = dist_class(**params)
    except Exception as e:
        raise ValueError(f"Failed to instantiate '{dist_type}' with {params}: {e}") from e
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


def simulate_driven(frequency: Dict[str, Any], severity, n_trials: int, rng: np.random.Generator,
                    freq_mult: np.ndarray, impact_mult: np.ndarray):
    """
    Like simulate_with_consequences, but each simulated year has its own multiplier on the
    frequency and on the loss per event (from the drivers behind the risk). `severity` is an
    impact spec or a consequences.Severity. Returns (annual losses, {term key: annual losses}).
    """
    validate_frequency(frequency)
    if frequency["type"] == "poisson":
        counts = rng.poisson(float(frequency["rate"]) * freq_mult)
    else:
        counts = (rng.random(n_trials) < np.minimum(float(frequency["p"]) * freq_mult, 1.0)).astype(np.int64)
    n = int(counts.sum())
    trial_index = np.repeat(np.arange(n_trials), counts)
    scale = impact_mult[trial_index]
    if hasattr(severity, "sample"):
        losses, parts = severity.sample(n, rng)
    else:
        losses, parts = sample_impact(severity, n, rng), {}
    total = np.bincount(trial_index, weights=losses * scale, minlength=n_trials)
    return total, {k: np.bincount(trial_index, weights=v * scale, minlength=n_trials) for k, v in parts.items()}


def driver_multipliers(links: List[Dict[str, Any]], shocks: Dict[str, np.ndarray], n_trials: int):
    """
    Yearly multipliers on frequency and loss from a risk's drivers. Each link has the driver's
    sigma (log-scale spread) and the sensitivities f and i; the multiplier of one link is
    exp(s*sigma*Z - (s*sigma)^2/2), so on average it is 1 (frequency times loss together, too): the
    drivers' levels are already in the specs, the variation only makes the years differ - together
    for all risks of a driver.
    """
    log_f = np.zeros(n_trials)
    log_i = np.zeros(n_trials)
    for link in links:
        z = shocks[link["id"]]
        af = float(link.get("f") or 0.0) * float(link["sigma"])
        ai = float(link.get("i") or 0.0) * float(link["sigma"])
        log_f += af * z - af * af / 2
        # frequency and loss move with the same shock; the extra -af*ai keeps E[frequency x loss] = 1,
        # so the expected annual loss stays the estimate's
        log_i += ai * z - ai * ai / 2 - af * ai
    return np.exp(log_f), np.exp(log_i)


def simulate_with_consequences(frequency: Dict[str, Any], severity, n_trials: int,
                               rng: np.random.Generator):
    """Like simulate_annual_loss for a consequences.Severity; also returns the annual loss per term key."""
    counts = sample_frequency(frequency, n_trials, rng)
    losses, parts = severity.sample(int(counts.sum()), rng)
    trial_index = np.repeat(np.arange(n_trials), counts)
    total = np.bincount(trial_index, weights=losses, minlength=n_trials)
    return total, {k: np.bincount(trial_index, weights=v, minlength=n_trials) for k, v in parts.items()}


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
                 business_units: Dict[str, Dict[str, Any]],
                 term_losses: Optional[Dict[str, np.ndarray]] = None,
                 term_info: Optional[Dict[str, Dict[str, Any]]] = None,
                 correlation: Optional[Dict[str, Any]] = None):
        self.correlation = correlation  # what was applied (pairs with achieved loss correlation)
        self.term_losses = term_losses or {}   # annual consequence losses per customer/service key
        self.term_info = term_info or {}
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

    def prob_exceed(self, losses: np.ndarray) -> Callable[[float], float]:
        return lambda x: float((losses > x).mean())

    def report(self, percentiles: Iterable[float] = DEFAULT_PERCENTILES,
               appetites: Optional[List[Dict[str, Any]]] = None,
               objectives: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """JSON-serialisable summary of the whole simulation (with appetites: whether each is kept)."""
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
        standalone_p99 = sum(float(np.percentile(x, 99)) for x in self.risk_losses.values())
        total_stats = loss_statistics(total, percentiles)
        report = {
            "n_trials": self.n_trials,
            "seed": self.seed,
            "total": total_stats,
            "diversification_benefit_p99": standalone_p99 - total_stats["p99"],
            "loss_exceedance_curve": loss_exceedance_curve(total),
            "risks": per_risk,
            "business_units": per_unit,
        }
        if self.term_losses:
            report["consequences"] = consequence_report(
                {k: loss_statistics(v, percentiles) for k, v in self.term_losses.items()}, self.term_info)
        if self.correlation:
            report["correlation"] = self.correlation
        if appetites:
            from .appetite import evaluate
            report["appetite"] = []
            for a in appetites:
                losses = self.unit_losses(a["business_unit_id"]) if a.get("business_unit_id") else total
                report["appetite"].append(evaluate(a, self.prob_exceed(losses), loss_statistics(losses, percentiles),
                                                   loss_exceedance_curve(losses)))
        if objectives:
            from .objectives import evaluate as evaluate_objective
            report["objectives"] = []
            for o in objectives:
                ids = [rid for rid in o["risk_ids"] if rid in self.risk_losses]
                losses = sum((self.risk_losses[rid] for rid in ids), np.zeros(self.n_trials))
                report["objectives"].append(evaluate_objective(
                    o, loss_statistics(losses, percentiles), self.prob_exceed(losses),
                    loss_exceedance_curve(losses), len(ids)))
        return report


def consequence_report(stats_by_key: Dict[str, Dict[str, float]],
                       info: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Annual consequence losses per customer and per service, largest expected loss first."""
    out: Dict[str, List[Dict[str, Any]]] = {"customers": [], "services": []}
    for key, s in stats_by_key.items():
        i = info[key]
        out["customers" if i["kind"] == "customer" else "services"].append(
            {"id": i["id"], "name": i["name"], "risk_count": i["risk_count"], **s})
    for rows in out.values():
        rows.sort(key=lambda r: r["mean"], reverse=True)
    return out


def term_index(risks: Dict[str, Dict[str, Any]], residual: bool):
    """The consequence terms of all risks, grouped by key (customer:<id> / service:<id>)."""
    from .consequences import Severity
    severities = {rid: Severity.from_risk(r, residual) for rid, r in risks.items()}
    info: Dict[str, Dict[str, Any]] = {}
    for sev in severities.values():
        for t in sev.terms:
            entry = info.setdefault(t["key"], {"kind": t["kind"], "id": t["id"], "name": t["name"], "risk_count": 0})
            entry["risk_count"] += 1
    return severities, info


def _stable_key(text: str) -> int:
    """Deterministic 32-bit integer from a string (unlike hash())."""
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")


def run_simulation(risks: Dict[str, Dict[str, Any]],
                   business_units: Dict[str, Dict[str, Any]],
                   n_trials: int = 10_000, seed: Optional[int] = None,
                   residual: bool = True,
                   correlations: Optional[List[Tuple[str, str, float]]] = None) -> SimulationResult:
    """
    Simulates every risk on its own; with `correlations` (risk a, risk b,
    coefficient) the annual losses are then reordered across trials so that
    bad years coincide as given (see dependence.py). Pairs naming a risk that
    is not in `risks` are ignored.

    With residual=True a risk's `residual_frequency` / `residual_impact`
    (after controls) is used when present, otherwise the inherent
    `frequency` / `impact`.
    """
    if n_trials <= 0:
        raise ValueError("n_trials must be positive")
    shared_rng = np.random.default_rng(seed)
    severities, info = term_index(risks, residual)
    losses = {}
    parts_by_risk: Dict[str, Dict[str, np.ndarray]] = {}
    shocks: Dict[str, np.ndarray] = {}   # one standard normal per driver and simulated year
    for risk in risks.values():
        for link in risk.get("drivers") or []:
            if link["id"] not in shocks and float(link.get("sigma") or 0) > 0:
                d_rng = (np.random.default_rng([seed, _stable_key("driver:" + link["id"])])
                         if seed is not None else shared_rng)
                shocks[link["id"]] = d_rng.standard_normal(n_trials)
    for risk_id, risk in risks.items():
        freq = (residual and risk.get("residual_frequency")) or risk["frequency"]
        imp = (residual and risk.get("residual_impact")) or risk["impact"]
        # With a seed, each risk gets its own stream derived from its ID, so
        # adding/changing one risk (or switching inherent/residual) does not
        # change the random numbers drawn for the others.
        rng = (np.random.default_rng([seed, _stable_key(risk_id)])
               if seed is not None else shared_rng)
        links = [link for link in risk.get("drivers") or [] if link["id"] in shocks]
        if links:
            fm, im = driver_multipliers(links, shocks, n_trials)
            sev = severities[risk_id] if severities[risk_id].has_consequences else imp
            losses[risk_id], parts = simulate_driven(freq, sev, n_trials, rng, fm, im)
            if parts:
                parts_by_risk[risk_id] = parts
        elif severities[risk_id].has_consequences:
            losses[risk_id], parts_by_risk[risk_id] = simulate_with_consequences(
                freq, severities[risk_id], n_trials, rng)
        else:  # unchanged path, so risks without consequences keep exactly the same random numbers
            losses[risk_id] = simulate_annual_loss(freq, imp, n_trials, rng)
    correlation = None
    pairs = [(a, b, float(r)) for a, b, r in correlations or [] if a in risks and b in risks and a != b]
    if pairs:
        from . import dependence
        rng = np.random.default_rng([seed, _stable_key("correlation")]) if seed is not None else shared_rng
        perms, adjusted = dependence.reorder(losses, pairs, rng)
        for rid, perm in perms.items():
            losses[rid] = losses[rid][perm]
            if rid in parts_by_risk:
                parts_by_risk[rid] = {k: v[perm] for k, v in parts_by_risk[rid].items()}
        correlation = {"applied": True, "adjusted": adjusted, "pairs": dependence.achieved(losses, pairs)}
    terms: Dict[str, np.ndarray] = {}
    for parts in parts_by_risk.values():
        for key, v in parts.items():
            terms[key] = terms.get(key, 0) + v
    return SimulationResult(n_trials, seed, losses, risks, business_units, terms, info, correlation)
