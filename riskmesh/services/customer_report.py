"""
Customer assurance report: everything one customer should know about the
services we deliver to it, on one page they can print or save as PDF.

- the services it uses and the suppliers behind them, by rank (DORA);
- the risks that can disrupt those services, with and without controls, and
  the controls with their latest test;
- our expected penalty exposure to the customer from outages;
- the objectives behind those services and whether they are within tolerance;
- incidents in the last 12 months that hit the customer, and whether we
  notified it in time;
- what is still missing (an LEI, an RTO, an exit plan ...).

The report carries the audit chain head it was made from and the latest
closed reporting period, and a SHA-256 of its own content, so a customer can
check later that the copy they hold is the one we issued.
"""

import html
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from ..domain import audit
from ..domain.models import Incident

REPORT_VERSION = "1"


class CustomerReport:
    """Mixin for RiskService."""

    def customer_report(self, customer_id: str) -> Dict[str, Any]:
        profile = self.customer_profile(customer_id)
        c = profile["customer"]
        service_ids = {s["id"] for s in profile["services"]}
        risk_ids = {r["id"] for r in profile["risks"]}
        tests = self.latest_control_tests()

        # outage penalties for this customer: from the analytic report's consequences
        exposure = None
        try:
            rep = self.simulation_report(engine="analytic")
            row = next((x for x in rep.get("consequences", {}).get("customers", []) if x["id"] == c["id"]), None)
            if row:
                exposure = {"mean": row["mean"], "p90": row["p90"], "p99": row["p99"]}
        except Exception:  # no risks yet, or nothing the analytic engine can compute
            pass

        objectives = []
        for o in self.objective_overview().get("objectives", []):
            if set(o["service_ids"]) & service_ids:
                f = o.get("figures") or {}
                tol = f.get("tolerance")
                objectives.append({"name": o["name"], "criticality": o["criticality"], "kpi": o.get("kpi"),
                                   "within_tolerance": None if not tol else not tol["breached"]})

        since = (date.today() - timedelta(days=365)).isoformat()
        g = self._graph()
        incidents = []
        for inc in sorted(self.repo.list(Incident), key=lambda i: i.detected_at):
            if inc.detected_at[:10] < since:
                continue
            d = self.incident_deadlines(inc.id, g=g)
            mine = next((x for x in d["deadlines"] if x.get("customer_id") == c["id"]), None)
            if mine is None:
                continue
            incidents.append({"title": inc.title, "detected_at": inc.detected_at, "status": inc.status,
                              "resolved_at": inc.resolved_at, "root_cause": inc.root_cause,
                              "notify_by": mine["due"], "notified_at": mine["sent_at"], "notification": mine["status"]})

        analysis = {}
        for key, scope in (("ours", f"customer:{c['id']}"), ("customer", f"customer-own:{c['id']}")):
            try:
                t = self.simulation_report(unit_id=scope)["total"]
                analysis[key] = {k: t[k] for k in ("mean", "p50", "p80", "p90", "p99")}
            except Exception:  # no risks, or no own loss per hour set
                analysis[key] = None

        dora = self.dora_extract(customer_id) if "DORA" in c["regulations"] else None
        gaps = list(dora["gaps"]) if dora else []
        gaps += [f["name"] + ": " + f["finding"] for f in profile["findings"]]

        closed = [p for p in self.list_periods() if p.status == "closed"]
        last_period = max(closed, key=lambda p: p.end) if closed else None
        seq, head = self._chain_head()
        us = self.our_entity()
        body = {
            "version": REPORT_VERSION,
            "customer": {"name": c["name"], "sector": c.get("sector"), "regulations": c["regulations"],
                         "critical_function": c["critical_function"], "contract_start": c.get("contract_start"),
                         "contract_end": c.get("contract_end"), "notification_hours": c.get("notification_hours"),
                         "audit_rights": c.get("audit_rights"), "exit_support": c.get("exit_support")},
            "provider": {"name": us.name if us else None, "lei": us.lei if us else None,
                         "country": us.country if us else None},
            "services": profile["services"],
            "suppliers": [{"name": s["name"], "depth": s["depth"], "country": s.get("country"), "tier": s.get("tier")}
                          for s in profile["suppliers"]],
            "supply_chain": dora["supply_chain"] if dora else None,
            "risks": [{"name": r["name"], "level": r.get("level"), "expected_annual_loss": r.get("expected_annual_loss")}
                      for r in profile["risks"]],
            "controls": [{"name": x["name"], "type": x["control_type"], "status": x["implementation_status"],
                          "last_test": (tests[x["id"]].date if x["id"] in tests else None),
                          "last_result": (tests[x["id"]].status if x["id"] in tests else None)}
                         for x in profile["controls"] if set(x["risk_ids"]) & risk_ids],
            "penalty_exposure": exposure,
            "analysis": analysis,
            "objectives": objectives,
            "incidents": incidents,
            "open_points": gaps,
            "integrity": {"audit_seq": seq, "audit_head": head,
                          "period": ({"key": last_period.key, "closed_at": last_period.closed_at,
                                      "facts_hash": last_period.facts_hash} if last_period else None)},
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "generated_by": self.actor,
        }
        return {**body, "report_hash": audit.sha256(body)}

    # -- printable page ---------------------------------------------------------------------------

    def customer_report_html(self, customer_id: str) -> str:
        r = self.customer_report(customer_id)
        e = html.escape

        def money(v) -> str:
            return "–" if v is None else f"{v:,.0f}".replace(",", ".")

        def table(headers: List[str], rows: List[List[Any]], empty: str = "None.") -> str:
            if not rows:
                return f"<p class='muted'>{e(empty)}</p>"
            head = "".join(f"<th>{e(h)}</th>" for h in headers)
            body = "".join("<tr>" + "".join(f"<td>{e('' if v is None else str(v))}</td>" for v in row) + "</tr>"
                           for row in rows)
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

        c, p = r["customer"], r["provider"]
        regs = ", ".join(x.replace("_", " ") for x in c["regulations"]) or "–"
        sections = [
            "<h2>Services we deliver to you</h2>" + table(
                ["Service", "Criticality", "Recovery time (RTO)", "Recovery point (RPO)"],
                [[s["name"], s["criticality"].title(), f"{s['rto_hours']:g} h" if s.get("rto_hours") is not None else "–",
                  f"{s['rpo_hours']:g} h" if s.get("rpo_hours") is not None else "–"] for s in r["services"]]),
            "<h2>Suppliers behind them</h2>" + (
                table(["Rank", "Provider", "LEI", "Serves"],
                      [[x["rank"], x["provider"], x["lei"] or "–", x["recipient"]] for x in r["supply_chain"]])
                if r["supply_chain"] else
                table(["Supplier", "Level", "Country"],
                      [[s["name"], "direct" if s["depth"] == 1 else "fourth party", s.get("country") or "–"]
                       for s in r["suppliers"]])),
            "<h2>Risks to these services</h2>" + table(
                ["Risk", "Level", "Expected loss / year (with controls)"],
                [[x["name"], x.get("level") or "–", money(x["expected_annual_loss"])] for x in r["risks"]]),
            "<h2>Controls and their latest test</h2>" + table(
                ["Control", "Type", "Status", "Last test", "Result"],
                [[x["name"], x["type"].title(), x["status"].replace("_", " ").title(), x["last_test"] or "–",
                  (x["last_result"] or "–").title()] for x in r["controls"]]),
        ]
        a = r.get("analysis") or {}
        if a.get("ours") or a.get("customer"):
            rows = []
            if a.get("customer"):
                x = a["customer"]
                rows.append(["Your loss from outages of our services", money(x["mean"]), money(x["p50"]),
                             money(x["p80"]), money(x["p99"])])
            if a.get("ours"):
                x = a["ours"]
                rows.append(["Our loss from the risks to your services", money(x["mean"]), money(x["p50"]),
                             money(x["p80"]), money(x["p99"])])
            sections.append("<h2>Annual loss (with controls)</h2>" + table(
                ["", "Expected", "Typical year (P50)", "Bad year (P80)", "1 in 100 (P99)"], rows))
        if r["penalty_exposure"]:
            x = r["penalty_exposure"]
            sections.append("<h2>Outage penalties to you (our exposure)</h2>"
                            f"<p>Expected {money(x['mean'])} a year; in a bad year (1 in 10) {money(x['p90'])}; "
                            f"1 in 100 {money(x['p99'])}.</p>")
        if r["objectives"]:
            sections.append("<h2>Our objectives for these services</h2>" + table(
                ["Objective", "Criticality", "KPI", "Within tolerance"],
                [[o["name"], o["criticality"].replace("_", " ").title(), o.get("kpi") or "–",
                  {True: "yes", False: "no", None: "no tolerance set"}[o["within_tolerance"]]] for o in r["objectives"]]))
        sections.append("<h2>Incidents in the last 12 months</h2>" + table(
            ["Incident", "Detected", "Status", "Notify by", "Notified", "On time"],
            [[i["title"], i["detected_at"].replace("T", " "), i["status"].title(), (i["notify_by"] or "–").replace("T", " "),
              (i["notified_at"] or "–").replace("T", " "),
              {"sent": "yes", "late": "late", "not_sent": "not notified", "overdue": "overdue"}.get(i["notification"], "–")]
             for i in r["incidents"]], "No incidents affected your services."))
        if r["open_points"]:
            sections.append("<h2>Open points</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in r["open_points"]) + "</ul>")
        integ = r["integrity"]
        period = (f"latest closed period {e(integ['period']['key'])} (facts {e((integ['period']['facts_hash'] or '')[:16])}…)"
                  if integ["period"] else "no closed reporting period yet")
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Assurance report – {e(c['name'])}</title>
<style>
 body {{ font: 14px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; color: #111827; max-width: 860px; margin: 2rem auto; padding: 0 1.5rem; }}
 h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.8rem; border-bottom: 1px solid #e5e7eb; padding-bottom: .2rem; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }} th, td {{ text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #f1f5f9; }}
 th {{ color: #475569; font-weight: 600; }} .muted {{ color: #64748b; }} .meta {{ color: #475569; }}
 footer {{ margin-top: 2.5rem; font-size: 11px; color: #64748b; border-top: 1px solid #e5e7eb; padding-top: .6rem; word-break: break-all; }}
 @media print {{ body {{ margin: 0; }} .noprint {{ display: none; }} h2 {{ break-after: avoid; }} tr {{ break-inside: avoid; }} }}
</style></head><body>
<p class="noprint muted">Use your browser's Print (Ctrl+P) and "Save as PDF" to keep a copy.</p>
<h1>Assurance report: {e(c['name'])}</h1>
<p class="meta">From {e(p['name'] or 'the provider')}{(' · LEI ' + e(p['lei'])) if p['lei'] else ''} · {e(r['generated_at'][:10])}<br>
Regulation: {e(regs)}{' · supports a critical or important function' if c['critical_function'] else ''}
{(' · contract ' + e(c['contract_start'] or '?') + ' – ' + e(c['contract_end'] or '?')) if (c['contract_start'] or c['contract_end']) else ''}
{(' · we notify incidents within ' + format(c['notification_hours'], 'g') + ' h') if c['notification_hours'] is not None else ''}</p>
{''.join(sections)}
<footer>Prepared from RiskMesh by {e(r['generated_by'])} at {e(r['generated_at'])}; audit chain entry {integ['audit_seq']} ({e(integ['audit_head'][:16])}…), {period}.<br>
Report fingerprint (SHA-256 of its content): {e(r['report_hash'])}</footer>
</body></html>"""
