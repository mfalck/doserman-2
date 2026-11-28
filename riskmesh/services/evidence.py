"""
Evidence: our own observed events and public benchmarks, and what they say
about the estimates.

An observation is evidence for a risk when it names the risk, or when its
category is one of the risk's evidence categories (or the risk's own
category) and its level fits the risk:

- at a supplier: the risk names that supplier (or one it subcontracts to);
- at a business unit: the unit is the risk's unit or one below it;
- at group level: the risk belongs to a top-level unit.

The period the data covers runs from when recording started - the first
entry in the sources those observations come from (or of those categories
when no source is given) - to today, unless `since` is given. A benchmark is
evidence when its category matches. See domain/calibration.py for the maths;
nothing changes until apply_calibration is called.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from ..domain import calibration as cal
from ..domain.errors import ValidationError
from ..domain.models import Benchmark, BusinessUnit, Observation, Risk, Supplier
from .core import ServiceCore

MAX_POINTS_PER_ROW = 1000


class Evidence(ServiceCore):
    """Mixin for RiskService (uses _create/_update/_delete/_require/repo/_descendant_ids/update_risk)."""

    # -- validation (called from RiskService._validate) --------------------------------

    def _validate_evidence(self, e) -> bool:
        if isinstance(e, Observation):
            if not e.category or not str(e.category).strip():
                raise ValidationError("An observation needs a category, e.g. 'data breach'.")
            e.category = str(e.category).strip()
            self._date(e.date, "date")
            e.date = str(e.date)[:10]
            if isinstance(e.count, bool) or not isinstance(e.count, (int, float)) or e.count < 0 \
                    or float(e.count) != int(e.count):
                raise ValidationError("count must be a whole number (0 or more).")
            e.count = int(e.count)
            self._non_negative(e.loss, "loss")
            if e.business_unit_id and e.supplier_id:
                raise ValidationError("An observation is at a business unit or at a supplier, not both.")
            return True
        if isinstance(e, Benchmark):
            self._name(e, "Benchmark")
            if not e.category or not str(e.category).strip():
                raise ValidationError("A benchmark needs a category, e.g. 'data breach'.")
            e.category = str(e.category).strip()
            self._non_negative(e.events_per_year, "events_per_year")
            self._non_negative(e.loss_p10, "loss_p10")
            self._non_negative(e.loss_p90, "loss_p90")
            if (e.loss_p10 is None) != (e.loss_p90 is None):
                raise ValidationError("Give both loss_p10 and loss_p90, or neither.")
            if e.loss_p10 is not None and not 0 < e.loss_p10 < e.loss_p90:
                raise ValidationError("loss_p10 must be positive and below loss_p90.")
            if e.events_per_year is None and e.loss_p10 is None:
                raise ValidationError("A benchmark needs events per year, a loss range, or both.")
            if isinstance(e.weight, bool) or not isinstance(e.weight, (int, float)) or e.weight <= 0:
                raise ValidationError("weight must be a positive number.")
            return True
        return False

    # -- CRUD ---------------------------------------------------------------------------------

    def create_observation(self, date: str, category: str, **optional: Any) -> Observation:
        return self._create(Observation, date=date, category=category, **optional)

    def get_observation(self, observation_id: str) -> Observation:
        return self._require(Observation, observation_id, "Observation")

    def list_observations(self, category: Optional[str] = None) -> List[Observation]:
        out = self.repo.list(Observation)
        if category:
            out = [o for o in out if o.category.lower() == category.lower()]
        return sorted(out, key=lambda o: o.date, reverse=True)

    def update_observation(self, observation_id: str, **changes: Any) -> Observation:
        return self._update(Observation, observation_id, changes)

    def delete_observation(self, observation_id: str) -> None:
        self._delete(Observation, observation_id)

    def create_benchmark(self, name: str, category: str, **optional: Any) -> Benchmark:
        return self._create(Benchmark, name=name, category=category, **optional)

    def get_benchmark(self, benchmark_id: str) -> Benchmark:
        return self._require(Benchmark, benchmark_id, "Benchmark")

    def list_benchmarks(self) -> List[Benchmark]:
        return sorted(self.repo.list(Benchmark), key=lambda b: (b.category.lower(), b.name))

    def update_benchmark(self, benchmark_id: str, **changes: Any) -> Benchmark:
        return self._update(Benchmark, benchmark_id, changes)

    def delete_benchmark(self, benchmark_id: str) -> None:
        self._delete(Benchmark, benchmark_id)

    def _delete_observations_at(self, field: str, value: str) -> None:
        """Observations at a deleted unit or supplier go with it (they must not become group-level)."""
        for o in self.repo.list(Observation, **{field: value}):
            self._delete(Observation, o.id)

    @staticmethod
    def observation_level(o: Observation) -> str:
        return "supplier" if o.supplier_id else "unit" if o.business_unit_id else "group"

    def evidence_categories(self) -> List[str]:
        """Every category used by observations, benchmarks or risks (for pick lists)."""
        cats = {o.category for o in self.repo.list(Observation)} | {b.category for b in self.repo.list(Benchmark)}
        for r in self.repo.list(Risk):
            cats.update(r.evidence_categories)
        return sorted(cats, key=str.lower)

    # -- matching -----------------------------------------------------------------------------

    @staticmethod
    def _categories(risk: Risk) -> set:
        cats = {c.strip().lower() for c in risk.evidence_categories if c and c.strip()}
        if risk.category:
            cats.add(risk.category.strip().lower())
        return cats

    def _matches(self, risk: Risk, observations: Sequence[Observation], units: Dict[str, BusinessUnit],
                 suppliers: Dict[str, Supplier]) -> List[Observation]:
        cats = self._categories(risk)
        below = set(self._descendant_ids(risk.business_unit_id, list(units.values())))
        top_level = not (units.get(risk.business_unit_id) and units[risk.business_unit_id].parent_id)
        chain = set(self._chain(risk.supplier_ids, suppliers)) if risk.supplier_ids else set()
        out = []
        for o in observations:
            if o.risk_id:
                if o.risk_id == risk.id:
                    out.append(o)
                continue
            if o.category.lower() not in cats:
                continue
            if o.supplier_id:
                hit = o.supplier_id in chain
            elif o.business_unit_id:
                hit = o.business_unit_id in below
            else:
                hit = top_level
            if hit:
                out.append(o)
        return out

    # -- calibration --------------------------------------------------------------------------

    def calibrate_risk(self, risk_id: str, expert_years: float = 2.0, expert_events: float = 5.0,
                       since: Optional[str] = None, today: Optional[date] = None) -> Dict[str, Any]:
        """What the evidence says about the risk's frequency and loss, and the suggested specs."""
        risk = self.get_risk(risk_id)
        if expert_years <= 0 or expert_events <= 0:
            raise ValidationError("The expert's weight must be positive.")
        today = today or date.today()
        observations = self.repo.list(Observation)
        units = {u.id: u for u in self.repo.list(BusinessUnit)}
        suppliers = {s.id: s for s in self.repo.list(Supplier)}
        matched = self._matches(risk, observations, units, suppliers)
        cats = self._categories(risk)
        if since:
            self._date(since, "since")
            start = since[:10]
        else:
            # recording started with the first entry in the sources these events come from
            # (an incident log kept since 2025 also says when nothing happened)
            relevant = [o for o in observations if o.category.lower() in cats] + matched
            sources = {o.source for o in relevant if o.source}
            dates = [o.date for o in relevant] + [o.date for o in observations if o.source in sources]
            start = min(dates) if dates else None
        in_window = [o for o in matched if start is None or o.date >= start]
        years = cal.years_between(start, today.isoformat()) if start else 0.0
        events = sum(o.count for o in in_window)
        benchmarks = [b for b in self.repo.list(Benchmark) if b.category.lower() in cats]

        freq_field = "residual_frequency" if risk.residual_frequency else "frequency"
        impact_field = "residual_impact" if risk.residual_impact else "impact"
        frequency = cal.calibrate_frequency(
            getattr(risk, freq_field), events, years,
            [(b.events_per_year, b.weight) for b in benchmarks if b.events_per_year is not None], expert_years)
        losses: List[float] = []
        for o in in_window:
            if o.loss is not None and o.count > 0:
                losses += [o.loss / o.count] * min(o.count, MAX_POINTS_PER_ROW)
        impact = cal.calibrate_impact(
            getattr(risk, impact_field), losses,
            [(b.loss_p10, b.loss_p90, b.weight) for b in benchmarks if b.loss_p10 is not None], expert_events)
        has_evidence = bool(in_window or benchmarks or years)
        return {
            "risk": {"id": risk.id, "name": risk.name, "category": risk.category,
                     "evidence_categories": sorted(cats)},
            "since": start, "years": round(years, 2), "has_evidence": has_evidence,
            "frequency_field": freq_field, "frequency": frequency,
            "impact_field": impact_field, "impact": impact,
            "impact_note": None if impact else "The loss is not a lognormal range (low P10 / high P90): "
                                               "compare it with the observed losses by hand.",
            "observations": [{"id": o.id, "date": o.date, "category": o.category, "count": o.count, "loss": o.loss,
                              "level": self.observation_level(o), "source": o.source} for o in in_window],
            "benchmarks": [{"id": b.id, "name": b.name, "source": b.source, "year": b.year,
                            "events_per_year": b.events_per_year, "loss_p10": b.loss_p10, "loss_p90": b.loss_p90,
                            "weight": b.weight} for b in benchmarks],
            "settings": {"expert_years": expert_years, "expert_events": expert_events},
        }

    def calibration_overview(self, expert_years: float = 2.0, expert_events: float = 5.0) -> List[Dict[str, Any]]:
        """One row per risk that has evidence: estimate, what the data says and the suggestion."""
        rows = []
        for risk in sorted(self.repo.list(Risk), key=lambda r: r.name):
            c = self.calibrate_risk(risk.id, expert_years, expert_events)
            if not c["has_evidence"]:
                continue
            f, i = c["frequency"], c["impact"]
            rows.append({"id": risk.id, "name": risk.name, "events": f["events"], "years": c["years"],
                         "benchmarks": len(c["benchmarks"]), "current_rate": f["current_rate"],
                         "observed_rate": f["observed_rate"], "calibrated_rate": f["calibrated_rate"],
                         "rate_change": f["change"], "impact_change": i["change_median"] if i else None,
                         "observed_losses": i["observed"]["count"] if i else 0})
        return rows

    def apply_calibration(self, risk_id: str, frequency: bool = True, impact: bool = True,
                          expert_years: float = 2.0, expert_events: float = 5.0, uncertain: bool = False) -> Risk:
        """Replaces the risk's (residual) frequency and/or loss with the suggestion; recorded with its basis."""
        c = self.calibrate_risk(risk_id, expert_years, expert_events)
        changes: Dict[str, Any] = {}
        if frequency:
            # uncertain: keep the remaining uncertainty about the rate (a negative binomial)
            suggested = c["frequency"].get("suggested_uncertain") if uncertain else None
            changes[c["frequency_field"]] = suggested or c["frequency"]["suggested"]
        if impact and c["impact"]:
            changes[c["impact_field"]] = c["impact"]["suggested"]
        if not changes:
            raise ValidationError("Nothing to apply.")
        basis = (f"calibrated from {c['frequency']['events']} observed events over {c['years']:g} years and "
                 f"{len(c['benchmarks'])} benchmarks (expert weight {expert_years:g} years / {expert_events:g} events)")
        with self.context(reason=f"{self.reason} - {basis}" if self.reason else basis):
            return self.update_risk(risk_id, **changes)


EVIDENCE_WRITE_METHODS = ("create_observation", "update_observation", "delete_observation", "create_benchmark",
                          "update_benchmark", "delete_benchmark", "apply_calibration")
