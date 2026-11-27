"""
Risk appetite: is the loss distribution within what the organisation accepts?

An appetite is a tolerance curve: points (loss, probability) meaning "we
accept at most this probability that the annual loss exceeds this amount".
Checked against the loss exceedance curve of its scope (the whole
organisation, or a business unit with its children). Both engines evaluate
it the same way, through a function giving P(annual loss > x).
"""

from typing import Any, Callable, Dict, List


def evaluate(appetite: Dict[str, Any], prob_exceed: Callable[[float], float],
             stats: Dict[str, float], curve: List[Dict[str, float]]) -> Dict[str, Any]:
    rows = []
    for p in sorted(appetite.get("points") or [], key=lambda p: p["loss"]):
        actual = float(prob_exceed(float(p["loss"])))
        tol = float(p["probability"])
        rows.append({"loss": float(p["loss"]), "tolerance": tol, "actual": actual, "breached": actual > tol,
                     "ratio": actual / tol if tol > 0 else None})
    return {
        "id": appetite["id"],
        "name": appetite["name"],
        "business_unit_id": appetite.get("business_unit_id"),
        "statement": appetite.get("statement"),
        "breached": any(r["breached"] for r in rows),
        "worst_ratio": max((r["ratio"] for r in rows if r["ratio"] is not None), default=None),
        "points": rows,
        "mean": stats["mean"],
        "p99": stats["p99"],
        "loss_exceedance_curve": curve,
    }
