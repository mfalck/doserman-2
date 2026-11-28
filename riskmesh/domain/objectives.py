"""
Risk to an objective: the annual loss of the risks that threaten it, and
whether that stays within the objective's tolerance. Both engines evaluate it
the same way, from the loss distribution of exactly those risks (with the
correlations between them in Monte Carlo).
"""

from typing import Any, Callable, Dict, List


def evaluate(objective: Dict[str, Any], stats: Dict[str, float], prob_exceed: Callable[[float], float],
             curve: List[Dict[str, float]], risk_count: int) -> Dict[str, Any]:
    loss, prob = objective.get("tolerance_loss"), objective.get("tolerance_probability")
    tolerance = None
    if loss is not None and prob is not None:
        actual = float(prob_exceed(float(loss)))
        tolerance = {"loss": float(loss), "probability": float(prob), "actual": actual, "breached": actual > prob}
    return {
        "id": objective["id"],
        "name": objective["name"],
        "criticality": objective.get("criticality"),
        "risk_count": risk_count,
        "mean": stats["mean"], "p90": stats["p90"], "p99": stats["p99"], "tvar99": stats["tvar99"],
        "prob_any_loss": stats["prob_any_loss"],
        "tolerance": tolerance,
        "loss_exceedance_curve": curve,
    }
