"""
Analytic (numerical) engine: exact compound-loss distributions via FFT.

For one risk the annual loss is S = X_1 + ... + X_N, with N the number of
events (Poisson or Bernoulli) and X the loss per event. Its distribution is
computed, not simulated:

1. The severity X is discretised on a grid 0, h, 2h, ... ("rounding": the
   probability of [(k-1/2)h, (k+1/2)h) is put on kh). The extreme tail beyond
   the 1 - 1e-6 quantile is put on that quantile, so no probability is lost
   and the grid does not have to reach astronomically far; this lowers the
   mean by far less than 0.1% even for heavy-tailed lognormal losses. The
   standard deviation of very heavy tails depends on those 1-in-a-million
   losses and can come out a few percent low; quantiles and TVaR 99 do not.
2. In Fourier space the compound distribution has a closed form:
       Poisson(lambda):  phi_S = exp(lambda * (phi_X - 1))
       Bernoulli(p):     phi_S = 1 - p + p * phi_X
3. Independent risks are added by multiplying their transforms; one inverse
   FFT gives the distribution of the sum (a business unit or the total).

The grid for each result is sized to what it contains, so a small risk gets
a fine grid and the total a coarser one. Accuracy is limited only by the
grid step h (reported as `grid_step`), not by sampling noise, and the same
input always gives exactly the same output.

Risks are independent of each other. Within one event, a risk's consequences
for several services and customers share one outage duration (common shock,
see consequences.py); that is part of the event's loss distribution, so the
method above still applies unchanged.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from scipy import stats

from .consequences import Severity
from .risk_model import (
    DEFAULT_PERCENTILES,
    build_impact_distribution,
    consequence_report,
    term_index,
    validate_frequency,
)

SEVERITY_EPS = 1e-6       # severity tail beyond this quantile is collapsed onto it
COUNT_EPS = 1e-9          # P(more events than the grid allows for)
DEFAULT_POINTS = 2 ** 19  # grid size for units / total (524,288 points)
ENGINE_VERSION = "analytic-1"  # bump when results change, so stored runs are not reused
RISK_POINTS = 2 ** 18     # grid size for a single risk


def _specs(risk: Dict[str, Any], residual: bool) -> Tuple[Dict[str, Any], Any]:
    freq = (residual and risk.get("residual_frequency")) or risk["frequency"]
    sev = Severity.from_risk(risk, residual)
    return freq, (sev if sev.has_consequences else sev.impact)


def _upper_bound(freq: Dict[str, Any], impact) -> float:
    """A loss level the risk's annual loss exceeds with probability ~TAIL_EPS."""
    validate_frequency(freq)
    if isinstance(impact, Severity):
        x_cap = impact.impact_cap(SEVERITY_EPS) + impact.consequence_cap()
    else:
        dist = build_impact_distribution(impact)
        x_cap = float(impact["value"]) if dist is None else float(dist.ppf(1 - SEVERITY_EPS))
    if freq["type"] == "poisson":
        n_cap = max(1.0, float(stats.poisson.ppf(1 - COUNT_EPS, float(freq["rate"]))))
    else:
        n_cap = 1.0
    bound = n_cap * max(x_cap, 0.0)
    if not np.isfinite(bound):
        raise ValueError(f"Impact distribution {impact} has no finite upper quantile; use Monte Carlo.")
    return bound


def _severity_pmf(impact, h: float, n: int) -> np.ndarray:
    """Severity probabilities on 0, h, ..., (n-1)h (rounding method, tail on the last point)."""
    if isinstance(impact, Severity):
        return impact.pmf(h, n, SEVERITY_EPS)
    pmf = np.zeros(n)
    dist = build_impact_distribution(impact)
    if dist is None:
        pmf[min(int(round(float(impact["value"]) / h)), n - 1)] = 1.0
        return pmf
    x_cap = float(dist.ppf(1 - SEVERITY_EPS))
    k_max = min(n - 1, max(1, int(np.ceil(x_cap / h))))
    cdf = dist.cdf((np.arange(k_max + 1) + 0.5) * h)  # F at the upper edge of each cell
    pmf[0] = cdf[0]                                    # includes any negative support
    pmf[1:k_max + 1] = np.diff(cdf)
    pmf[k_max] += 1.0 - cdf[-1]                        # extreme tail, so total mass is exactly 1
    return pmf


def _transform(freq: Dict[str, Any], impact: Dict[str, Any], h: float, n: int) -> np.ndarray:
    """Fourier transform of one risk's annual-loss distribution on the grid."""
    phi_x = np.fft.rfft(_severity_pmf(impact, h, n))
    if freq["type"] == "poisson":
        return np.exp(float(freq["rate"]) * (phi_x - 1.0))
    p = float(freq["p"])
    return 1.0 - p + p * phi_x


def _prob_no_event(freq: Dict[str, Any]) -> float:
    return float(np.exp(-float(freq["rate"]))) if freq["type"] == "poisson" else 1.0 - float(freq["p"])


class LossDistribution:
    """A discrete annual-loss distribution on the grid 0, h, 2h, ..."""

    def __init__(self, pmf: np.ndarray, h: float, prob_no_event: Optional[float] = None):
        pmf = np.clip(pmf, 0.0, None)  # remove tiny negative FFT round-off
        self.pmf = pmf / pmf.sum()
        self.h = h
        # exact P(no event at all); the grid alone would also count losses below h/2 as zero
        self.prob_no_event = float(self.pmf[0]) if prob_no_event is None else prob_no_event
        self.cdf = np.cumsum(self.pmf)
        self.x = np.arange(len(pmf)) * h

    def quantile(self, q: float) -> float:
        return float(self.x[min(int(np.searchsorted(self.cdf, q - 1e-12)), len(self.x) - 1)])

    def tvar(self, q: float) -> float:
        """Expected shortfall: mean of the worst (1-q) of outcomes (atom at VaR split exactly)."""
        k = min(int(np.searchsorted(self.cdf, q - 1e-12)), len(self.x) - 1)
        tail = float(np.dot(self.x[k + 1:], self.pmf[k + 1:]))
        tail += self.x[k] * (self.cdf[k] - q)
        return tail / (1.0 - q)

    def prob_exceed(self, x: float) -> float:
        """P(annual loss > x) on the grid."""
        k = int(np.floor(x / self.h + 1e-9))
        if k < 0:
            return 1.0
        return float(max(0.0, 1.0 - self.cdf[min(k, len(self.cdf) - 1)]))

    def statistics(self, percentiles: Iterable[float] = DEFAULT_PERCENTILES, tail_level: float = 99) -> Dict[str, float]:
        mean = float(np.dot(self.x, self.pmf))
        var = float(np.dot((self.x - mean) ** 2, self.pmf))
        support = np.nonzero(self.pmf > 1e-12)[0]
        out = {
            "mean": mean,
            "std": var ** 0.5,
            "max": float(self.x[support[-1]]) if support.size else 0.0,
            "prob_any_loss": 1.0 - self.prob_no_event,
        }
        for p in percentiles:
            out[f"p{p:g}"] = self.quantile(p / 100)
        out[f"tvar{tail_level:g}"] = self.tvar(tail_level / 100)
        out["grid_step"] = self.h
        return out

    def exceedance_curve(self, n_points: int = 20) -> List[Dict[str, float]]:
        """Like the Monte Carlo curve: thresholds at quantiles of the positive losses."""
        p0 = self.pmf[0]
        if p0 >= 1.0 - 1e-12:
            return []
        cond = (self.cdf[1:] - p0) / (1.0 - p0)
        out, seen = [], set()
        for q in np.linspace(0, 0.999, n_points):
            k = 1 + min(int(np.searchsorted(cond, q)), len(cond) - 1)
            if k not in seen:
                seen.add(k)
                out.append({"loss": float(self.x[k]), "prob_exceed": float(1.0 - self.cdf[k])})
        return out


def aggregate(specs: List[Tuple[Dict[str, Any], Dict[str, Any]]], n_points: int = DEFAULT_POINTS) -> LossDistribution:
    """Distribution of the sum of independent risks given as (frequency, impact) pairs."""
    if not specs:
        return LossDistribution(np.array([1.0, 0.0]), 1.0)
    bound = sum(_upper_bound(f, i) for f, i in specs)
    h = max(bound * 1.05 / n_points, 1e-9)  # 5% headroom so the sum does not wrap around
    phi = np.ones(n_points // 2 + 1, dtype=complex)
    p_none = 1.0
    for f, i in specs:
        phi *= _transform(f, i, h, n_points)
        p_none *= _prob_no_event(f)
    return LossDistribution(np.fft.irfft(phi, n=n_points), h, p_none)


def _descendants(units: Dict[str, Dict[str, Any]], unit_id: str) -> List[str]:
    children: Dict[Optional[str], List[str]] = {}
    for uid, u in units.items():
        children.setdefault(u.get("parent_id"), []).append(uid)
    out, stack = [], [unit_id]
    while stack:
        cur = stack.pop()
        if cur not in out:
            out.append(cur)
            stack.extend(children.get(cur, []))
    return out


def analyse(risks: Dict[str, Dict[str, Any]], business_units: Dict[str, Dict[str, Any]],
            residual: bool = True, n_points: int = DEFAULT_POINTS,
            percentiles: Iterable[float] = DEFAULT_PERCENTILES,
            appetites: Optional[List[Dict[str, Any]]] = None,
            correlated: int = 0, objectives: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Same report structure as the Monte Carlo SimulationResult.report(),
    plus `engine` and grid information. The analytic engine adds up
    independent risks; `correlated` (the number of correlation pairs among
    these risks) is only reported, as a warning that Monte Carlo is needed.
    """
    percentiles = tuple(percentiles)
    specs = {rid: _specs(r, residual) for rid, r in risks.items()}

    per_risk = []
    standalone_p99 = 0.0
    for rid, r in risks.items():
        dist = aggregate([specs[rid]], RISK_POINTS)
        s = dist.statistics(percentiles)
        standalone_p99 += s["p99"]
        per_risk.append({"id": rid, "name": r["name"], "business_unit_id": r.get("business_unit_id"), **s})
    per_risk.sort(key=lambda r: r["mean"], reverse=True)

    # The same set of risks often appears several times (a unit without children has
    # own == rolled-up; the top unit usually equals the total), so compute each set once.
    cache: Dict[frozenset, LossDistribution] = {}

    def dist_of(risk_ids: Iterable[str]) -> LossDistribution:
        key = frozenset(risk_ids)
        if key not in cache:
            cache[key] = aggregate([specs[rid] for rid in sorted(key)], n_points)
        return cache[key]

    by_unit: Dict[str, List[str]] = {}
    for rid, r in risks.items():
        by_unit.setdefault(r.get("business_unit_id"), []).append(rid)
    per_unit = []
    for uid, u in business_units.items():
        own = by_unit.get(uid, [])
        rolled = [rid for sub in _descendants(business_units, uid) for rid in by_unit.get(sub, [])]
        per_unit.append({
            "id": uid, "name": u["name"], "parent_id": u.get("parent_id"),
            "own": dist_of(own).statistics(percentiles),
            "rolled_up": dist_of(rolled).statistics(percentiles),
        })

    total = dist_of(risks.keys())
    total_stats = total.statistics(percentiles)
    report = {
        "engine": "analytic",
        "n_trials": None,
        "seed": None,
        "grid_points": n_points,
        "total": total_stats,
        "diversification_benefit_p99": standalone_p99 - total_stats["p99"],
        "loss_exceedance_curve": total.exceedance_curve(),
        "risks": per_risk,
        "business_units": per_unit,
    }
    severities, info = term_index(risks, residual)
    if info:  # the consequences per customer / service, each summed over the risks that cause them
        stats_by_key = {}
        for key in info:
            parts = [(specs[rid][0], sev.only([key])) for rid, sev in severities.items()
                     if any(t["key"] == key for t in sev.terms)]
            stats_by_key[key] = aggregate(parts, RISK_POINTS).statistics(percentiles)
        report["consequences"] = consequence_report(stats_by_key, info)
    if correlated:
        report["correlation"] = {"applied": False, "pair_count": correlated,
                                 "note": "The analytic engine treats risks as independent; "
                                         "use Monte Carlo to apply the correlations."}
    if appetites:
        from .appetite import evaluate
        report["appetite"] = []
        for a in appetites:
            uid = a.get("business_unit_id")
            dist = dist_of([rid for sub in _descendants(business_units, uid) for rid in by_unit.get(sub, [])]
                           if uid else risks.keys())
            report["appetite"].append(evaluate(a, dist.prob_exceed, dist.statistics(percentiles),
                                               dist.exceedance_curve()))
    if objectives:
        from .objectives import evaluate as evaluate_objective
        report["objectives"] = []
        for o in objectives:
            ids = [rid for rid in o["risk_ids"] if rid in risks]
            dist = dist_of(ids)
            report["objectives"].append(evaluate_objective(o, dist.statistics(percentiles), dist.prob_exceed,
                                                           dist.exceedance_curve(), len(ids)))
    return report
