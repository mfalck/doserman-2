"""
Period close and reporting (mixed into RiskService).

Closing a period freezes its figures: the analysis is run (and stored as a
named run), a flat fact table is written for the total, every business unit
(including sub-units), every category and every risk, the facts' SHA-256 is
recorded in the audit trail, and - if configured - the audit chain is
anchored. Reports (trend, comparison) only read these frozen facts, so they
always show what was reported, however the data changes afterwards.

The bridge explains the change in expected annual loss (after controls)
between two periods:
    total(B) - total(A) = new risks - removed risks + changed estimates - change in control effect
where "changed estimates" is the change in the inherent loss of risks present
in both periods and "control effect" is inherent minus residual loss.
"""

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from ..domain import audit
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..domain.models import BusinessUnit, Control, PeriodFact, ReportingPeriod, RiskEvent
from ..domain.periods import parse_period
from .core import ServiceCore

FACT_METRICS = ("risk_count", "high_risk_count", "eal_inherent", "eal_residual", "p50", "p90", "p99", "tvar99",
                "p99_inherent", "score_inherent", "score_residual", "controls", "control_effectiveness",
                "events", "actual_loss")


def _facts_hash(facts: List[PeriodFact]) -> str:
    rows = sorted((f.to_dict() for f in facts), key=lambda d: (d["level"], d["entity_id"] or ""))
    for r in rows:  # ids and timestamps are storage details, not figures
        for k in ("id", "created_at", "updated_at"):
            r.pop(k, None)
    return audit.sha256(rows)


def _avg(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


class Reporting(ServiceCore):

    # -- periods -------------------------------------------------------------------

    def list_periods(self) -> List[ReportingPeriod]:
        return sorted(self.repo.list(ReportingPeriod), key=lambda p: (p.end, p.period_type), reverse=True)

    def get_period(self, key: str) -> ReportingPeriod:
        normalised = parse_period(key)[0]
        found = self.repo.list(ReportingPeriod, key=normalised)
        if not found:
            raise NotFoundError(f"Period {normalised} has not been closed.")
        return found[0]

    def close_period(self, key: str, note: Optional[str] = None) -> ReportingPeriod:
        """Freezes the figures of a period. Closing again needs reopen_period() first."""
        key, ptype, start, end = parse_period(key)
        existing = self.repo.list(ReportingPeriod, key=key)
        if existing and existing[0].status == "closed":
            raise ConflictError(f"Period {key} is already closed (reopen it first to close it again).")
        result = self.run_analysis(mode="compare", engine="analytic", label=f"Period {key}")
        facts = self._build_facts(key, start, end, result)
        period = self._store_period(existing[0] if existing else None, key, ptype, start, end, result["run"]["id"],
                                    facts, note)
        if self.anchoring_enabled() and self.anchor_on_period_close:
            anchor = self.create_anchor(reason=f"period {key} closed")
            period = self._set_period_anchor(period.id, anchor.id)
        return period

    def _store_period(self, existing, key, ptype, start, end, run_id, facts, note) -> ReportingPeriod:
        before = ReportingPeriod.from_dict(existing.to_dict()) if existing else None
        period = existing or ReportingPeriod(key=key, period_type=ptype, start=start.isoformat(), end=end.isoformat())
        period.status = "closed"
        period.closed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        period.closed_by = self.actor
        period.run_id = run_id
        period.note = note
        period.anchor_id = None
        period.facts_hash = _facts_hash(facts)
        for fact in facts:
            self.repo.save(fact)
        total = next(f for f in facts if f.level == "total")
        self._save(period, "UPDATE" if before else "CREATE", before,
                   details=f"period {key} closed: {total.risk_count} risks, expected loss "
                           f"{total.eal_residual:,.0f}, {len(facts)} facts")
        return period

    def _set_period_anchor(self, period_id: str, anchor_id: str) -> ReportingPeriod:
        period = self._require(ReportingPeriod, period_id, "Period")
        before = ReportingPeriod.from_dict(period.to_dict())
        period.anchor_id = anchor_id
        self._save(period, "UPDATE", before, details=f"period {period.key} anchored")
        return period

    def reopen_period(self, key: str) -> ReportingPeriod:
        """Removes the frozen facts so the period can be closed again. Recorded in the audit trail."""
        period = self.get_period(key)
        if period.status != "closed":
            raise ConflictError(f"Period {period.key} is not closed.")
        before = ReportingPeriod.from_dict(period.to_dict())
        for fact in self.repo.list(PeriodFact, period_key=period.key):
            self.repo.delete(PeriodFact, fact.id)
        period.status = "open"
        period.facts_hash = None
        self._save(period, "UPDATE", before, details=f"period {period.key} reopened")
        return period

    def period_facts(self, key: Optional[str] = None, level: Optional[str] = None) -> List[PeriodFact]:
        filters = {k: v for k, v in (("period_key", parse_period(key)[0] if key else None), ("level", level)) if v}
        facts = self.repo.list(PeriodFact, **filters)
        return sorted(facts, key=lambda f: (f.period_end, f.level, f.name or ""))

    def check_periods(self) -> List[Dict[str, Any]]:
        """For verify_audit(): the stored facts of every closed period must match the recorded hash."""
        errors = []
        for p in self.repo.list(ReportingPeriod):
            if p.status == "closed" and _facts_hash(self.repo.list(PeriodFact, period_key=p.key)) != p.facts_hash:
                errors.append({"entity_type": "reporting_period", "entity_id": p.id,
                               "error": f"the frozen figures of period {p.key} were changed"})
        return errors

    # -- building the fact table ---------------------------------------------------------

    def _build_facts(self, key: str, start: date, end: date, result: Dict[str, Any]) -> List[PeriodFact]:
        end_s = end.isoformat()
        residual = {r["id"]: r for r in result["residual"]["risks"]}
        inherent = {r["id"]: r for r in result["inherent"]["risks"]}
        units = {u.id: u for u in self.repo.list(BusinessUnit)}
        controls = self.repo.list(Control)
        events = [e for e in self.repo.list(RiskEvent) if start.isoformat() <= e.date[:10] <= end_s]
        ev_count, ev_loss = defaultdict(int), defaultdict(float)
        for e in events:
            ev_count[e.risk_id] += 1
            ev_loss[e.risk_id] += e.financial_impact or 0.0

        facts: List[PeriodFact] = []
        risk_rows = {}
        for r in self.list_risk_views():
            inh, res = r["scores"]["inherent"], r["scores"]["residual"]
            sim = residual.get(r["id"], {})
            fact = PeriodFact(
                period_key=key, period_end=end_s, level="risk", entity_id=r["id"], name=r["name"],
                business_unit_id=r["business_unit_id"], category=r.get("category"), risk_count=1,
                high_risk_count=int(inh["score"] >= 9), eal_inherent=inh["expected_annual_loss"],
                eal_residual=res["expected_annual_loss"], p50=sim.get("p50"), p90=sim.get("p90"),
                p99=sim.get("p99"), tvar99=sim.get("tvar99"), p99_inherent=inherent.get(r["id"], {}).get("p99"),
                likelihood_inherent=inh["likelihood"], impact_inherent=inh["impact"], score_inherent=inh["score"],
                likelihood_residual=res["likelihood"], impact_residual=res["impact"], score_residual=res["score"],
                controls=len(r["control_ids"]), events=ev_count[r["id"]], actual_loss=ev_loss[r["id"]])
            facts.append(fact)
            risk_rows[r["id"]] = fact

        def summary(rows: List[PeriodFact], **fields) -> PeriodFact:
            return PeriodFact(
                period_key=key, period_end=end_s, risk_count=len(rows),
                high_risk_count=sum(f.high_risk_count for f in rows),
                eal_inherent=sum(f.eal_inherent for f in rows), eal_residual=sum(f.eal_residual for f in rows),
                score_inherent=_avg([f.score_inherent for f in rows]),
                score_residual=_avg([f.score_residual for f in rows]),
                events=sum(f.events for f in rows), actual_loss=sum(f.actual_loss for f in rows), **fields)

        sim_units_inh = {u["id"]: u for u in result["inherent"]["business_units"]}
        for u in result["residual"]["business_units"]:
            subtree = set(self._descendant_ids(u["id"]))
            rows = [f for f in risk_rows.values() if f.business_unit_id in subtree]
            stats = u["rolled_up"]
            facts.append(summary(rows, level="unit", entity_id=u["id"], name=units[u["id"]].name if u["id"] in units
                                 else u["name"], business_unit_id=u["parent_id"], p50=stats["p50"], p90=stats["p90"],
                                 p99=stats["p99"], tvar99=stats["tvar99"],
                                 p99_inherent=sim_units_inh.get(u["id"], {}).get("rolled_up", {}).get("p99"),
                                 controls=sum(1 for c in controls if c.business_unit_id in subtree)))

        by_category: Dict[str, List[PeriodFact]] = defaultdict(list)
        for f in risk_rows.values():
            by_category[f.category or "uncategorised"].append(f)
        for cat, rows in sorted(by_category.items()):
            facts.append(summary(rows, level="category", entity_id=cat, name=cat, category=cat))

        total = result["residual"]["total"]
        facts.append(summary(list(risk_rows.values()), level="total", name="Total", p50=total["p50"],
                             p90=total["p90"], p99=total["p99"], tvar99=total["tvar99"],
                             p99_inherent=result["inherent"]["total"]["p99"], controls=len(controls),
                             control_effectiveness=self.control_effectiveness()))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for f in facts:
            f.created_at = f.updated_at = now
        return facts

    # -- reports ---------------------------------------------------------------------------------

    def trend(self, metric: str = "eal_residual", level: str = "total",
              entity_id: Optional[str] = None, period_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """One value per closed period, oldest first (e.g. expected loss of a unit per quarter)."""
        if metric not in FACT_METRICS:
            raise ValidationError(f"metric must be one of {', '.join(FACT_METRICS)}.")
        closed = {p.key: p for p in self.repo.list(ReportingPeriod) if p.status == "closed"
                  and (period_type is None or p.period_type == period_type)}
        rows = [f for f in self.repo.list(PeriodFact, level=level)
                if f.period_key in closed and (entity_id is None or f.entity_id == entity_id)]
        if level != "total" and entity_id is None:
            raise ValidationError("Give entity_id for unit, category or risk trends.")
        return [{"period": f.period_key, "period_end": f.period_end, "value": getattr(f, metric)}
                for f in sorted(rows, key=lambda f: f.period_end)]

    def compare_periods(self, a: str, b: str) -> Dict[str, Any]:
        """Risk-by-risk comparison of two closed periods, with the bridge and heat-map moves."""
        pa, pb = self.get_period(a), self.get_period(b)
        for p in (pa, pb):
            if p.status != "closed":
                raise ConflictError(f"Period {p.key} is not closed.")
        fa = {(f.level, f.entity_id): f for f in self.repo.list(PeriodFact, period_key=pa.key)}
        fb = {(f.level, f.entity_id): f for f in self.repo.list(PeriodFact, period_key=pb.key)}

        def diff_rows(level: str) -> List[Dict[str, Any]]:
            keys = sorted({k for k in fa if k[0] == level} | {k for k in fb if k[0] == level},
                          key=lambda k: ((fb.get(k) or fa.get(k)).name or ""))
            out = []
            for k in keys:
                x, y = fa.get(k), fb.get(k)
                row = {"id": k[1], "name": (y or x).name, "status": "new" if x is None else "removed" if y is None
                       else "changed" if any(getattr(x, m) != getattr(y, m) for m in ("eal_inherent", "eal_residual"))
                       else "unchanged"}
                for m in ("eal_inherent", "eal_residual", "p99", "score_inherent", "score_residual", "actual_loss"):
                    va, vb = getattr(x, m) if x else None, getattr(y, m) if y else None
                    row[m] = {"a": va, "b": vb, "change": (vb or 0) - (va or 0) if va is not None or vb is not None
                              else None}
                if level == "risk":
                    row["heatmap"] = {"a": [x.likelihood_residual, x.impact_residual] if x else None,
                                      "b": [y.likelihood_residual, y.impact_residual] if y else None}
                out.append(row)
            return out

        risks = diff_rows("risk")
        new = sum(r["eal_residual"]["b"] for r in risks if r["status"] == "new")
        removed = sum(r["eal_residual"]["a"] for r in risks if r["status"] == "removed")
        common = [r for r in risks if r["status"] in ("changed", "unchanged")]
        estimates = sum(r["eal_inherent"]["change"] for r in common)
        control_effect = sum((r["eal_inherent"]["b"] - r["eal_residual"]["b"])
                             - (r["eal_inherent"]["a"] - r["eal_residual"]["a"]) for r in common)
        ta, tb = fa[("total", None)], fb[("total", None)]
        return {
            "a": pa.key, "b": pb.key,
            "total": diff_rows("total")[0],
            "bridge": {k: v + 0.0 for k, v in (  # + 0.0 turns -0.0 into 0.0
                ("start", ta.eal_residual), ("new_risks", new), ("removed_risks", -removed),
                ("changed_estimates", estimates), ("controls", -control_effect), ("end", tb.eal_residual))},
            "risks": risks,
            "units": diff_rows("unit"),
            "categories": diff_rows("category"),
            "heatmap_moves": [{"id": r["id"], "name": r["name"], **r["heatmap"]} for r in risks
                              if r["heatmap"]["a"] != r["heatmap"]["b"]],
        }


def facts_to_csv(facts: List[PeriodFact]) -> str:
    """All facts as CSV (UTF-8 with BOM, so Excel shows æøå correctly)."""
    import csv
    import io
    columns = [c for c in PeriodFact.field_names() if c not in ("id", "created_at", "updated_at")]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(columns)
    for f in facts:
        row = []
        for c in columns:
            v = getattr(f, c)
            text = "" if v is None else str(v)
            row.append("'" + text if text[:1] in ("=", "+", "-", "@") and not text[1:2].isdigit() else text)
        writer.writerow(row)
    return "\ufeff" + buf.getvalue()


REPORTING_WRITE_METHODS = ("_store_period", "_set_period_anchor", "reopen_period")
