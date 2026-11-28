"""
NIS2 significance: how often a single incident is big enough to be significant.

NIS2 art. 23(3) calls an incident significant when it causes (or can cause)
severe disruption or financial loss; for several digital providers
Implementing Regulation (EU) 2024/2690 adds thresholds such as a direct loss
above EUR 500,000 (or 5% of turnover). That is about ONE event, not a year,
so it is the loss per event that matters here, not annual percentiles.

Per risk: the share of its events whose loss exceeds the threshold (or whose
outage lasts longer than the hour threshold), times its expected number of
events per year, gives the expected significant incidents per year; and the
chance of at least one. Shares are estimated from 20,000 sampled events per
risk with a fixed seed, so the figures are repeatable. A working aid: whether
an incident is significant is decided on the facts, with the authority's
guidance.
"""

import hashlib
import math
from typing import Any, Dict, Optional

import numpy as np

from ..domain.consequences import Severity, term_value
from ..domain.models import Risk
from ..domain.risk_model import sample_impact

SAMPLES = 20_000


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")


def significant_share(risk: Dict[str, Any], residual: bool, loss: Optional[float], hours: Optional[float],
                      n: int = SAMPLES) -> float:
    """The share of one risk's events that are significant (loss above `loss` or outage above `hours`)."""
    rng = np.random.default_rng(_seed("significance:" + str(risk.get("id"))))
    sev = Severity.from_risk(risk, residual)
    x = sample_impact(sev.impact, n, rng) if sev.impact else np.zeros(n)
    duration = (residual and risk.get("residual_outage_hours")) or risk.get("outage_hours")
    d = np.maximum(sample_impact(duration, n, rng), 0.0) if duration else np.zeros(n)
    total = x + sum((term_value(t, d) for t in sev.terms), np.zeros(n))
    hit = np.zeros(n, dtype=bool)
    if loss is not None:
        hit |= total > loss
    if hours is not None and duration:
        hit |= d > hours
    return float(hit.mean())


def per_year(freq: Dict[str, Any], share: float):
    """(expected significant incidents per year, chance of at least one) for a frequency spec."""
    if freq["type"] == "bernoulli":
        p = float(freq["p"])
        return p * share, p * share
    m = float(freq["rate"]) * share
    if freq["type"] == "negbin":  # thinning a negative binomial keeps its shape
        k = float(freq["shape"])
        return m, 1.0 - (k / (k + m)) ** k
    return m, 1.0 - math.exp(-m)


class Significance:
    """Mixin for RiskService (uses engine_risks and the configured thresholds)."""

    significant_loss: Optional[float] = None
    significant_outage_hours: Optional[float] = None

    def significance_report(self, residual: bool = True, loss: Optional[float] = None,
                            hours: Optional[float] = None) -> Dict[str, Any]:
        loss = self.significant_loss if loss is None else loss
        hours = self.significant_outage_hours if hours is None else hours
        risks = self.engine_risks(self.repo.list(Risk))
        rows = []
        p_none = 1.0
        expected_total = 0.0
        if loss is not None or hours is not None:
            for rid, r in risks.items():
                freq = (residual and r.get("residual_frequency")) or r["frequency"]
                share = significant_share(r, residual, loss, hours)
                expected, p_any = per_year(freq, share)
                expected_total += expected
                p_none *= 1.0 - p_any
                if expected > 0:
                    rows.append({"id": rid, "name": r["name"], "share_of_events": share,
                                 "expected_per_year": expected, "prob_at_least_one": p_any})
        rows.sort(key=lambda x: x["expected_per_year"], reverse=True)
        return {
            "threshold": {"loss": loss, "outage_hours": hours},
            "residual": residual,
            "risks": rows,
            "total": {"expected_per_year": expected_total, "prob_at_least_one": 1.0 - p_none,
                      "once_in_years": (1.0 / expected_total) if expected_total > 0 else None},
            "note": ("A single incident is counted as significant above the threshold; NIS2 art. 23(3) and, for "
                     "some providers, Implementing Regulation (EU) 2024/2690. Confirm the thresholds that apply."),
        }
