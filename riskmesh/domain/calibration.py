"""
Calibrating an estimate with data: our own observations and public benchmarks.

Frequency (events per year), Poisson-Gamma. The expert's estimate is the
starting point, worth `expert_years` years of data. Each benchmark adds
pseudo-data (its rate times its weight in years), and our own observations
add n events over T years:

    alpha = rate0 * w0 + sum(rate_b * w_b) + n
    beta  =         w0 + sum(w_b)          + T
    calibrated rate = alpha / beta,  90% interval from Gamma(alpha, beta)

So a few years of our own data move the estimate a little, many years a lot,
and a benchmark counts as much as the weight it is given.

Loss per event (lognormal, as low P10 / high P90): credibility weighting on
the log scale. The expert's centre and spread count as `expert_events`
events, a benchmark as its weight in events, and each observed loss as one
event. The spread pools each source's own spread with how far the centres
disagree, so conflicting sources widen the range.

Only a suggestion: nothing changes until someone accepts it.
"""

import math
from typing import Any, Dict, Optional, Sequence, Tuple

from scipy import stats

Z90 = stats.norm.ppf(0.9)   # 1.2816: P10/P90 are mu -/+ Z90 * sigma


def rate_of(freq: Dict[str, Any]) -> float:
    """Expected events per year of a frequency spec (Bernoulli p -> the rate with the same P(no event))."""
    if freq["type"] in ("poisson", "negbin"):
        return float(freq["rate"])
    p = min(float(freq["p"]), 0.999999)
    return -math.log(1.0 - p)


def spec_like(freq: Dict[str, Any], rate: float) -> Dict[str, Any]:
    """A frequency spec of the same type as `freq` with this rate."""
    if freq["type"] == "poisson":
        return {"type": "poisson", "rate": round(rate, 6)}
    if freq["type"] == "negbin":
        return {"type": "negbin", "rate": round(rate, 6), "shape": freq["shape"]}
    return {"type": "bernoulli", "p": round(1.0 - math.exp(-rate), 6)}


def calibrate_frequency(freq: Dict[str, Any], events: int, years: float,
                        benchmarks: Sequence[Tuple[float, float]], expert_years: float = 2.0) -> Dict[str, Any]:
    """benchmarks: (events per year, weight in years)."""
    rate0 = rate_of(freq)
    alpha = rate0 * expert_years + sum(r * w for r, w in benchmarks) + events
    beta = expert_years + sum(w for _, w in benchmarks) + years
    rate = alpha / beta
    low, high = (stats.gamma.ppf(q, alpha, scale=1 / beta) for q in (0.05, 0.95)) if alpha > 0 else (0.0, 0.0)
    observed = events / years if years > 0 else None
    return {
        "current_rate": rate0, "observed_rate": observed, "calibrated_rate": rate,
        "interval": [float(low), float(high)], "events": events, "years": years,
        "weights": {"expert_years": expert_years, "benchmark_years": sum(w for _, w in benchmarks),
                    "data_years": years},
        "suggested": spec_like(freq, rate),
        # the posterior predictive: a Poisson whose rate has the remaining uncertainty Gamma(alpha, beta)
        "suggested_uncertain": ({"type": "negbin", "rate": round(rate, 6), "shape": round(alpha, 6)}
                                if alpha > 0 else None),
        "change": rate / rate0 - 1 if rate0 > 0 else None,
    }


def _log_params(p10: float, p90: float) -> Tuple[float, float]:
    mu = (math.log(p10) + math.log(p90)) / 2
    return mu, (math.log(p90) - math.log(p10)) / (2 * Z90)


def calibrate_impact(impact: Dict[str, Any], losses: Sequence[float],
                     benchmarks: Sequence[Tuple[float, float, float]], expert_events: float = 5.0) -> Optional[Dict[str, Any]]:
    """
    impact must be lognorm_p10_p90 (otherwise None: compare by hand).
    losses: observed loss per event. benchmarks: (p10, p90, weight in events).
    """
    if impact.get("dist") != "lognorm_p10_p90":
        return None
    mu0, s0 = _log_params(float(impact["p10"]), float(impact["p90"]))
    parts = [(mu0, s0 ** 2, expert_events)]
    for p10, p90, w in benchmarks:
        mu, s = _log_params(p10, p90)
        parts.append((mu, s ** 2, w))
    logs = [math.log(x) for x in losses if x and x > 0]
    if logs:
        m = sum(logs) / len(logs)
        var = sum((v - m) ** 2 for v in logs) / (len(logs) - 1) if len(logs) > 1 else s0 ** 2
        parts.append((m, var, float(len(logs))))
    total = sum(w for _, _, w in parts)
    mu = sum(mu_ * w for mu_, _, w in parts) / total
    # pooled spread: within-source variance plus how far the centres disagree
    var = sum((v + (mu_ - mu) ** 2) * w for mu_, v, w in parts) / total
    sigma = math.sqrt(var)
    p10, p90 = math.exp(mu - Z90 * sigma), math.exp(mu + Z90 * sigma)
    return {
        "current": {"p10": impact["p10"], "p90": impact["p90"], "median": math.exp(mu0)},
        "observed": {"count": len(logs), "median": math.exp(sorted(logs)[len(logs) // 2]) if logs else None,
                     "mean": sum(losses) / len(losses) if losses else None},
        "suggested": {"dist": "lognorm_p10_p90", "p10": round(p10, 2), "p90": round(p90, 2)},
        "weights": {"expert_events": expert_events, "benchmark_events": sum(w for _, _, w in benchmarks),
                    "observed_events": len(logs)},
        "change_median": math.exp(mu) / math.exp(mu0) - 1,
    }


def years_between(first: str, last: str) -> float:
    from datetime import date
    d0, d1 = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])
    return max((d1 - d0).days + 1, 1) / 365.25
