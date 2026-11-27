"""
The portfolio view of risk: correlations between risks (bad years that come
together) and the risk appetite the loss distribution is checked against.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..domain.errors import ConflictError, ValidationError
from ..domain.models import Appetite, Correlation

MAX_COEFFICIENT = 0.99


class Portfolio:
    """Mixin for RiskService (uses _create/_update/_delete/_require/repo)."""

    # -- validation (called from RiskService._validate) --------------------------------

    def _validate_portfolio(self, e) -> bool:
        if isinstance(e, Correlation):
            if not e.risk_a_id or not e.risk_b_id:
                raise ValidationError("A correlation needs two risks.")
            if e.risk_a_id == e.risk_b_id:
                raise ValidationError("A risk cannot be correlated with itself.")
            if isinstance(e.coefficient, bool) or not isinstance(e.coefficient, (int, float)) \
                    or not -MAX_COEFFICIENT <= e.coefficient <= MAX_COEFFICIENT:
                raise ValidationError(f"coefficient must be a number from -{MAX_COEFFICIENT} to {MAX_COEFFICIENT}.")
            e.coefficient = float(e.coefficient)
            return True
        if isinstance(e, Appetite):
            self._name(e, "Appetite")
            points = []
            for p in e.points or []:
                try:
                    loss, prob = float(p["loss"]), float(p["probability"])
                except (KeyError, TypeError, ValueError):
                    raise ValidationError("Each appetite point needs a loss and a probability.") from None
                if loss <= 0:
                    raise ValidationError("Appetite losses must be positive.")
                if not 0 < prob < 1:
                    raise ValidationError("Appetite probabilities must be between 0 and 1 (e.g. 0.01 for 1%).")
                points.append({"loss": loss, "probability": prob})
            if not points:
                raise ValidationError("An appetite needs at least one point (loss and probability).")
            points.sort(key=lambda p: p["loss"])
            for a, b in zip(points, points[1:]):
                if a["loss"] == b["loss"]:
                    raise ValidationError(f"Two appetite points for the same loss ({a['loss']:,.0f}).")
                if b["probability"] > a["probability"]:
                    raise ValidationError("The accepted probability cannot rise for a larger loss.")
            e.points = points
            return True
        return False

    def _check_pair_unique(self, c: Correlation) -> None:
        pair = {c.risk_a_id, c.risk_b_id}
        for other in self.repo.list(Correlation):
            if other.id != c.id and {other.risk_a_id, other.risk_b_id} == pair:
                raise ConflictError("These two risks already have a correlation; change that one instead.")

    # -- correlations -----------------------------------------------------------------------

    def create_correlation(self, risk_a_id: str, risk_b_id: str, coefficient: float, **optional: Any) -> Correlation:
        self._check_pair_unique(Correlation(risk_a_id=risk_a_id, risk_b_id=risk_b_id, coefficient=coefficient))
        return self._create(Correlation, risk_a_id=risk_a_id, risk_b_id=risk_b_id, coefficient=coefficient,
                            **optional)

    def get_correlation(self, correlation_id: str) -> Correlation:
        return self._require(Correlation, correlation_id, "Correlation")

    def list_correlations(self, risk_id: Optional[str] = None) -> List[Correlation]:
        out = self.repo.list(Correlation)
        if risk_id:
            out = [c for c in out if risk_id in (c.risk_a_id, c.risk_b_id)]
        return sorted(out, key=lambda c: -abs(c.coefficient))

    def update_correlation(self, correlation_id: str, **changes: Any) -> Correlation:
        current = self.get_correlation(correlation_id)
        candidate = Correlation.from_dict({**current.to_dict(), **changes})
        self._check_pair_unique(candidate)
        return self._update(Correlation, correlation_id, changes)

    def delete_correlation(self, correlation_id: str) -> None:
        self._delete(Correlation, correlation_id)

    def correlation_pairs(self, risk_ids=None) -> List[Tuple[str, str, float]]:
        """(risk a, risk b, coefficient) for the engine, limited to risk_ids when given."""
        keep = set(risk_ids) if risk_ids is not None else None
        return [(c.risk_a_id, c.risk_b_id, c.coefficient)
                for c in sorted(self.repo.list(Correlation), key=lambda c: c.id)
                if keep is None or (c.risk_a_id in keep and c.risk_b_id in keep)]

    # -- appetite ---------------------------------------------------------------------------

    def create_appetite(self, name: str, points: List[Dict[str, float]], **optional: Any) -> Appetite:
        return self._create(Appetite, name=name, points=points, **optional)

    def get_appetite(self, appetite_id: str) -> Appetite:
        return self._require(Appetite, appetite_id, "Appetite")

    def list_appetites(self) -> List[Appetite]:
        return sorted(self.repo.list(Appetite), key=lambda a: (a.business_unit_id is not None, a.name))

    def update_appetite(self, appetite_id: str, **changes: Any) -> Appetite:
        return self._update(Appetite, appetite_id, changes)

    def delete_appetite(self, appetite_id: str) -> None:
        self._delete(Appetite, appetite_id)

    def appetites_for(self, unit_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        The appetites a report covers: all of them for the whole organisation;
        for a report on one unit, those for that unit or a unit below it.
        """
        apps = sorted(self.repo.list(Appetite), key=lambda a: a.id)
        if unit_id:
            below = set(self._descendant_ids(unit_id))
            apps = [a for a in apps if a.business_unit_id in below]
        return [{"id": a.id, "name": a.name, "business_unit_id": a.business_unit_id, "points": a.points,
                 "statement": a.statement} for a in apps]

    def _delete_unit_appetites(self, unit_id: str) -> None:
        """An appetite for a unit goes with the unit (it must not silently become organisation-wide)."""
        for a in self.repo.list(Appetite, business_unit_id=unit_id):
            self._delete(Appetite, a.id)


PORTFOLIO_WRITE_METHODS = (
    "create_correlation", "update_correlation", "delete_correlation",
    "create_appetite", "update_appetite", "delete_appetite",
)
