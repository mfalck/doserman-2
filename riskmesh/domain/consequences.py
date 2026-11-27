"""
Common shock: one event, losses in many places at once.

A risk can say how long an event takes something down (`outage_hours`, a
distribution in hours, same formats as an impact). The service layer links
the risk to the services it disrupts (directly, or because a supplier in
their chain fails) and to the customers of those services, and turns that
into consequence *terms*:

    our own cost        per_hour x hours                       (per service)
    customer penalty    min(cap, per_hour x max(0, hours - grace))  (per customer)

The loss of one event is then

    X + sum over terms of term(D)

with X the risk's ordinary impact and D ONE duration drawn for the event and
shared by every term. That shared D is the point: an outage of a supplier
behind three customers hits all three in the same event, so their penalties
add up in the tail instead of averaging out as independent risks would.
Different events (and different risks) are still independent.

Both engines use this class: Monte Carlo samples it, the analytic engine
asks for its distribution on a grid.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from .risk_model import build_impact_distribution, sample_impact

QUANTILE_POINTS = 20_000   # duration discretised by this many equally likely points
DURATION_EPS = 1e-6        # extreme duration tail collapsed onto this quantile (like the analytic engine)


def term_value(term: Dict[str, Any], hours):
    """Loss of one consequence term for outage duration(s) `hours`."""
    hours = np.asarray(hours, dtype=float)
    value = float(term.get("per_hour") or 0.0) * np.maximum(0.0, hours - float(term.get("grace") or 0.0))
    cap = term.get("cap")
    return np.minimum(value, float(cap)) if cap is not None else value


class Severity:
    """The loss of one event: an ordinary impact plus consequences of a shared outage duration."""

    def __init__(self, impact: Optional[Dict[str, Any]], duration: Optional[Dict[str, Any]] = None,
                 terms: Iterable[Dict[str, Any]] = ()):
        self.impact = impact                      # None = no ordinary loss (only consequences)
        self.terms = list(terms) if duration else []
        self.duration = duration if self.terms else None
        self._points: Optional[Tuple[np.ndarray, np.ndarray]] = None

    @classmethod
    def from_risk(cls, risk: Dict[str, Any], residual: bool) -> "Severity":
        impact = (residual and risk.get("residual_impact")) or risk["impact"]
        duration = (residual and risk.get("residual_outage_hours")) or risk.get("outage_hours")
        terms = (risk.get("consequences") or {}).get("terms", [])
        return cls(impact, duration, terms)

    def only(self, keys: Iterable[str]) -> "Severity":
        """The consequences for some terms only (e.g. one customer), without the ordinary impact."""
        keys = set(keys)
        return Severity(None, self.duration, [t for t in self.terms if t["key"] in keys])

    @property
    def has_consequences(self) -> bool:
        return bool(self.terms)

    # -- the duration, discretised ----------------------------------------------------------

    def _duration_points(self) -> Tuple[np.ndarray, np.ndarray]:
        """(hours, probability) of equally likely duration points (one point for a fixed duration)."""
        if self._points is None:
            dist = build_impact_distribution(self.duration)
            if dist is None:
                hours = np.array([float(self.duration["value"])])
            else:
                u = (np.arange(QUANTILE_POINTS) + 0.5) / QUANTILE_POINTS
                hours = dist.ppf(np.minimum(u, 1 - DURATION_EPS))
            hours = np.maximum(hours, 0.0)
            self._points = (hours, np.full(len(hours), 1.0 / len(hours)))
        return self._points

    def consequence_points(self, terms: Optional[List[Dict[str, Any]]] = None) -> Tuple[np.ndarray, np.ndarray]:
        hours, prob = self._duration_points()
        values = sum((term_value(t, hours) for t in (self.terms if terms is None else terms)), np.zeros(len(hours)))
        return values, prob

    # -- summary numbers ----------------------------------------------------------------------

    def _impact_dist(self):
        return build_impact_distribution(self.impact) if self.impact else None

    def impact_mean(self) -> float:
        if not self.impact:
            return 0.0
        dist = self._impact_dist()
        return float(self.impact["value"]) if dist is None else float(dist.mean())

    def impact_cap(self, eps: float) -> float:
        if not self.impact:
            return 0.0
        dist = self._impact_dist()
        return float(self.impact["value"]) if dist is None else float(dist.ppf(1 - eps))

    def consequence_mean(self, terms: Optional[List[Dict[str, Any]]] = None) -> float:
        if not self.terms:
            return 0.0
        values, prob = self.consequence_points(terms)
        return float(np.dot(values, prob))

    def consequence_cap(self) -> float:
        if not self.terms:
            return 0.0
        values, _ = self.consequence_points()
        return float(values.max())

    def mean(self) -> float:
        return self.impact_mean() + self.consequence_mean()

    def median(self) -> float:
        """Median loss of one event (exact without consequences, on a fine grid with them)."""
        if not self.terms:
            dist = self._impact_dist()
            return float(self.impact["value"]) if dist is None else float(dist.median())
        n = 2 ** 14
        cap = self.impact_cap(DURATION_EPS) + self.consequence_cap()
        if cap <= 0:
            return 0.0
        h = cap / (n // 2)
        cdf = np.cumsum(self.pmf(h, n, DURATION_EPS))
        return float(np.searchsorted(cdf, 0.5 - 1e-12) * h)

    # -- on a grid (analytic engine) -------------------------------------------------------------

    def pmf(self, h: float, n: int, eps: float) -> np.ndarray:
        """Probabilities of the event loss on 0, h, ..., (n-1)h."""
        base = np.zeros(n)
        if not self.impact:
            base[0] = 1.0
        else:
            dist = self._impact_dist()
            if dist is None:
                base[min(int(round(float(self.impact["value"]) / h)), n - 1)] = 1.0
            else:
                x_cap = float(dist.ppf(1 - eps))
                k_max = min(n - 1, max(1, int(np.ceil(x_cap / h))))
                cdf = dist.cdf((np.arange(k_max + 1) + 0.5) * h)
                base[0] = cdf[0]
                base[1:k_max + 1] = np.diff(cdf)
                base[k_max] += 1.0 - cdf[-1]
        if not self.terms:
            return base
        values, prob = self.consequence_points()
        cons = np.bincount(np.minimum(np.rint(values / h).astype(np.int64), n - 1), weights=prob, minlength=n)
        if not self.impact:
            return cons
        out = np.fft.irfft(np.fft.rfft(base) * np.fft.rfft(cons), n=n)  # the caller's grid has room for both
        return np.clip(out, 0.0, None)

    # -- sampling (Monte Carlo) --------------------------------------------------------------------

    def sample(self, n: int, rng: np.random.Generator) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        """Losses of n events, and the part of them that falls on each term (by term key)."""
        x = sample_impact(self.impact, n, rng) if self.impact else np.zeros(n)
        if not self.terms or n == 0:
            return x, {}
        hours = np.maximum(sample_impact(self.duration, n, rng), 0.0)
        parts = {t["key"]: term_value(t, hours) for t in self.terms}
        return x + sum(parts.values()), parts


def stress(terms: List[Dict[str, Any]], hours: float) -> List[Dict[str, Any]]:
    """What each term costs for an outage of exactly `hours` (a deterministic stress test)."""
    return [{**t, "loss": float(term_value(t, hours))} for t in terms]
