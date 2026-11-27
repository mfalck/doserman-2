"""
Command line interface (local presentation tier).

  py -m riskmesh demo                     load the example organisation
  py -m riskmesh tree                     show the organisation
  py -m riskmesh risks                    list risks with expected annual loss
  py -m riskmesh simulate [--compare]     Monte Carlo report
  py -m riskmesh export FILE / import FILE
  py -m riskmesh serve [--port 8000]      start the REST API

The backend is chosen with RISKMESH_BACKEND or --backend (json, sql, neo4j).
"""

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import logging
import sys
from typing import Any, Dict, List

from ..bootstrap import make_service
from ..config import FILE_NAME, load_settings, user_config_dir, write_template
from ..domain.errors import DomainError, NotFoundError, ValidationError
from ..services import RiskService
from ..services.demo import seed_demo


def money(value: float) -> str:
    return f"{value:>14,.0f}"


def print_report(report: Dict[str, Any], title: str) -> None:
    t = report["total"]
    if report.get("engine") == "analytic":
        method = f"analytic, grid step {t['grid_step']:,.0f}"
    else:
        method = f"Monte Carlo, {report['n_trials']:,} simulated years"
    print(f"\n=== {title} ({method}) ===")
    print(f"Expected annual loss : {money(t['mean'])}")
    print(f"Median (P50)         : {money(t['p50'])}")
    print(f"P90                  : {money(t['p90'])}")
    print(f"VaR 99 (P99)         : {money(t['p99'])}")
    print(f"TVaR 99              : {money(t['tvar99'])}")
    print(f"Diversification P99  : {money(report['diversification_benefit_p99'])}")

    print(f"\n{'Risk':<32}{'EAL':>14}{'P90':>14}{'P99':>14}{'P(loss)':>9}")
    for r in report["risks"]:
        print(f"{r['name'][:31]:<32}{money(r['mean'])}{money(r['p90'])}{money(r['p99'])}"
              f"{r['prob_any_loss']:>9.1%}")

    print(f"\n{'Business unit (incl. children)':<32}{'EAL':>14}{'P90':>14}{'P99':>14}")
    for u in sorted(report["business_units"], key=lambda u: u["rolled_up"]["mean"], reverse=True):
        ru = u["rolled_up"]
        if ru["mean"] > 0:
            print(f"{u['name'][:31]:<32}{money(ru['mean'])}{money(ru['p90'])}{money(ru['p99'])}")

    cons = report.get("consequences")
    if cons:
        print(f"\n{'Outage consequences':<32}{'EAL':>14}{'P90':>14}{'P99':>14}  (included above)")
        for kind, label in (("customers", "penalty"), ("services", "own cost")):
            for c in cons[kind]:
                print(f"{(label + ' ' + c['name'])[:31]:<32}{money(c['mean'])}{money(c['p90'])}{money(c['p99'])}")

    corr = report.get("correlation")
    if corr and not corr["applied"]:
        print(f"\nNote: {corr['pair_count']} correlations are not applied. {corr['note']}")
    elif corr:
        print(f"\n{len(corr['pairs'])} correlations applied" +
              (" (the pairs did not fit together and were adjusted to the nearest valid set)" if corr["adjusted"] else ""))

    for a in report.get("appetite", []):
        print(f"\nAppetite: {a['name']} - {'OUTSIDE' if a['breached'] else 'within'}")
        print(f"  {'Loss above':>16}{'Accepted':>10}{'Actual':>10}")
        for pt in a["points"]:
            print(f"  {money(pt['loss']):>16}{pt['tolerance']:>10.2%}{pt['actual']:>10.2%}"
                  f"{'  exceeded' if pt['breached'] else ''}")


def print_tree(nodes: List[Dict[str, Any]], indent: int = 0) -> None:
    for n in nodes:
        print("  " * indent + f"- {n['name']}  ({n['id'][:8]})")
        print_tree(n["children"], indent + 1)


def _resolve_run(svc: RiskService, ref: str) -> str:
    """Accepts a run number ("17" or "#17") or an id."""
    ref = ref.lstrip("#")
    if ref.isdigit():
        for r in svc.list_runs(limit=100_000):
            if r["number"] == int(ref):
                return r["id"]
        raise NotFoundError(f"Run #{ref} not found.")
    return ref


def _ask_password() -> str:
    first = getpass.getpass("Password (at least 10 characters): ")
    if getpass.getpass("Repeat password: ") != first:
        raise ValidationError("The passwords do not match.")
    return first


def _user_by_name(svc: RiskService, username: str):
    found = [u for u in svc.list_users() if u.username == username]
    if not found:
        raise NotFoundError(f"No user '{username}'.")
    return found[0]


def _excel(args, svc: RiskService) -> int:
    import hashlib
    if args.excel_command == "export":
        with open(args.file, "wb") as f:
            f.write(svc.export_excel())
        print(f"Workbook written to {args.file}")
        return 0
    with open(args.file, "rb") as f:
        content = f.read()
    preview = svc.import_excel(content, replace=args.replace, dry_run=True)
    print(f"{'Sheet':<16}{'new':>6}{'changed':>9}{'same':>7}{'removed':>9}")
    for kind, c in preview["changes"].items():
        print(f"{kind.replace('_', ' '):<16}{c['created']:>6}{c['updated']:>9}{c['unchanged']:>7}{c['removed']:>9}")
    total = sum(c["created"] + c["updated"] + c["removed"] for c in preview["changes"].values())
    if args.dry_run or not total:
        print("Nothing changed." if not total else "Dry run: nothing stored.")
        return 0
    if not args.yes and input(f"Store these {total} changes? [y/N] ").strip().lower() not in ("y", "yes", "j", "ja"):
        print("Cancelled.")
        return 1
    with svc.context(source=f"excel:sha256:{hashlib.sha256(content).hexdigest()}"):
        svc.import_excel(content, replace=args.replace)
    print("Stored (recorded in the audit log).")
    return 0


def _find(items, name: str):
    hits = [x for x in items if x.id == name or x.name.lower() == name.lower()]
    return hits[0] if hits else None


def _third_party(args, svc: RiskService) -> int:
    cmd = args.tp_command
    if cmd == "overview":
        o = svc.third_party_overview()
        t = o["totals"]
        print(f"{t['suppliers']} suppliers, {t['services']} services, {t['customers']} customers "
              f"({t['regulated_customers']} regulated, {t['critical_function_customers']} with a critical function)")
        print(f"\n{'Supplier':<28}{'Tier':<11}{'Services':>9}{'Customers':>10}{'Contract value':>16}  Regulations")
        for x in o["suppliers"]:
            regs = ", ".join(f"{k} {v}" for k, v in x["by_regulation"].items())
            print(f"{x['name'][:27]:<28}{x['tier']:<11}{x['service_count']:>9}{x['customer_count']:>10}"
                  f"{money(x['contract_value'])}  {regs}")
        print(f"\n{'Service':<28}{'Criticality':<12}{'Suppliers':>10}{'Customers':>10}{'Contract value':>16}  Notify within")
        for x in o["services"]:
            h = x["shortest_notification_hours"]
            print(f"{x['name'][:27]:<28}{x['criticality']:<12}{x['supplier_count']:>10}{x['customer_count']:>10}"
                  f"{money(x['contract_value'])}  {'' if h is None else f'{h:g} h'}")
    elif cmd == "dora":
        cust = _find(svc.list_customers(), args.customer)
        if not cust:
            print(f"Error: no customer called {args.customer!r}", file=sys.stderr)
            return 1
        x = svc.dora_extract(cust.id)
        p = x["provider"]
        print(f"DORA register information for {cust.name} - provider {p['name']} (LEI {p['lei'] or 'missing'})")
        print(f"\n{'Rank':<6}{'Provider':<28}{'LEI / company no.':<24}{'Country':<9}Serves")
        for r in x["supply_chain"]:
            prov = next((q for q in x["providers"] if q["name"] == r["provider"]), {})
            ident = r["lei"] or prov.get("company_number") or "-"
            print(f"{r['rank']:<6}{r['provider'][:27]:<28}{ident:<24}{prov.get('country') or '-':<9}"
                  f"{r['recipient']}  ({r['service']})")
        if x["gaps"]:
            print("\nStill missing:")
            for gap in x["gaps"]:
                print(f"  - {gap}")
        if args.xlsx:
            with open(args.xlsx, "wb") as f:
                f.write(svc.export_dora_extract(cust.id))
            print(f"\nWorkbook written to {args.xlsx}")
    elif cmd == "findings":
        for f in svc.third_party_findings():
            print(f"{f['severity'].upper():<7}{f['entity_type']:<10}{f['name'][:30]:<31}{f['finding']}")
    elif cmd == "exposure":
        sup, ser = _find(svc.list_suppliers(), args.name), _find(svc.list_services(), args.name)
        if not sup and not ser:
            print(f"Error: no supplier or service called {args.name!r}", file=sys.stderr)
            return 1
        e = svc.supplier_exposure(sup.id) if sup else svc.service_exposure(ser.id)
        print(f"If {(sup or ser).name} fails: {e['customer_count']} customers, contract value"
              f"{money(e['contract_value'])}, {e['critical_function_customers']} with a critical function")
        if e["shortest_notification_hours"] is not None:
            print(f"Shortest notification deadline: {e['shortest_notification_hours']:g} hours")
        for x in e.get("via_suppliers", []):
            print(f"  via supplier   {x['name']} ({'subcontractor' if x['distance'] == 1 else 'further up the chain'})")
        for x in e.get("services", []):
            print(f"  service        {x['name']} ({x['criticality']})")
        for x in e.get("suppliers", []):
            level = "direct" if x["depth"] == 1 else f"level {x['depth']}"
            print(f"  depends on     {x['name']} ({x['tier']}, {level})")
        for c in e["customers"]:
            print(f"  customer       {c['name']} {', '.join(c['regulations'])}")
    elif cmd == "stress":
        sup, ser = _find(svc.list_suppliers(), args.name), _find(svc.list_services(), args.name)
        if not sup and not ser:
            print(f"Error: no supplier or service called {args.name!r}", file=sys.stderr)
            return 1
        st = svc.stress_test(args.hours, supplier_id=sup.id if sup else None, service_id=ser.id if ser else None)
        print(f"{st['source']['name']} down for {args.hours:g} hours: total{money(st['total'])}")
        for row in st["costs"]:
            what = "own cost" if row["kind"] == "service" else "penalty"
            print(f"  {what:<9} {row['name'][:30]:<31}{money(row['loss'])}")
        if st["notification_deadlines_missed"]:
            print("  Notification deadline shorter than the outage: " + ", ".join(st["notification_deadlines_missed"]))
        if st["customers_without_penalty_terms"]:
            print("  No penalty terms registered for: " + ", ".join(st["customers_without_penalty_terms"]))
    elif cmd == "customer":
        c = _find(svc.list_customers(), args.name)
        if not c:
            print(f"Error: no customer called {args.name!r}", file=sys.stderr)
            return 1
        p = svc.customer_profile(c.id)
        print(f"{c.name}  {c.sector or ''}  {', '.join(c.regulations)}"
              + ("  (critical function)" if c.critical_function else ""))
        for x in p["services"]:
            print(f"  service        {x['name']} ({x['criticality']}, RTO {x['rto_hours'] if x['rto_hours'] is not None else '?'} h)")
        for x in p["suppliers"]:
            print(f"  supplier       {x['name']} ({x['tier']}, {'direct' if x['depth'] == 1 else 'fourth party'}, "
                  f"{x['country'] or '?'})")
        for r in p["risks"]:
            print(f"  risk           {r['name']} ({r['level']}, expected loss{money(r['expected_annual_loss'])})")
        for f in p["findings"]:
            print(f"  {f['severity'].upper():<7}        {f['name']}: {f['finding']}")
    return 0


def _period(args, svc: RiskService) -> int:
    cmd = args.period_command
    if cmd == "close":
        p = svc.close_period(args.key, args.note)
        print(f"Closed {p.key} ({p.start} – {p.end}); figures frozen" + (", audit chain anchored" if p.anchor_id else ""))
    elif cmd == "reopen":
        print(f"Reopened {svc.reopen_period(args.key).key}")
    elif cmd == "list":
        for p in svc.list_periods():
            total = svc.period_facts(p.key, "total")
            eal = money(total[0].eal_residual).strip() if total else "–"
            print(f"{p.key:<9} {p.status:<7} {(p.closed_at or '')[:10]}  {p.closed_by or '':<12} expected loss {eal}"
                  + (f"  ({p.note})" if p.note else ""))
    elif cmd == "compare":
        c = svc.compare_periods(args.a, args.b)
        b = c["bridge"]
        print(f"Expected annual loss (after controls) {c['a']} -> {c['b']}")
        for label, key in (("Start", "start"), ("+ new risks", "new_risks"), ("- removed risks", "removed_risks"),
                           ("+/- changed estimates", "changed_estimates"), ("+/- controls", "controls"),
                           ("End", "end")):
            print(f"  {label:<24}{money(b[key])}")
        print(f"\n{'Risk':<30}{'Status':<10}{c['a']:>14}{c['b']:>14}{'Change':>14}")
        for r in c["risks"]:
            e = r["eal_residual"]
            print(f"{r['name'][:29]:<30}{r['status']:<10}{money(e['a'] or 0)}{money(e['b'] or 0)}{money(e['change'])}")
    elif cmd == "trend":
        for row in svc.trend(args.metric, args.level, args.entity):
            print(f"{row['period']:<9} {money(row['value']) if row['value'] is not None else '–'}")
    elif cmd == "export":
        from ..services.reporting import facts_to_csv
        with open(args.file, "w", encoding="utf-8", newline="") as f:
            f.write(facts_to_csv(svc.period_facts()))
        print(f"Period facts written to {args.file}")
    return 0


def _users(args, svc: RiskService) -> int:
    cmd = args.users_command
    if cmd == "list":
        for u in svc.list_users():
            print(f"{u.username:<24} {u.role:<7} {'active' if u.active else 'BLOCKED':<8} {u.auth_source:<6} "
                  f"{u.display_name or ''}")
    elif cmd in ("create-admin", "create"):
        role = "admin" if cmd == "create-admin" else args.role
        user = svc.create_user(args.username, role, _ask_password(), getattr(args, "name", None),
                               getattr(args, "email", None))
        print(f"Created {user.role} '{user.username}'.")
    elif cmd == "password":
        svc.set_password(_user_by_name(svc, args.username).id, _ask_password())
        print("Password changed; existing logins of this user were ended.")
    elif cmd == "role":
        print(f"'{svc.update_user(_user_by_name(svc, args.username).id, role=args.role).username}' is now {args.role}.")
    elif cmd in ("deactivate", "activate"):
        svc.update_user(_user_by_name(svc, args.username).id, active=cmd == "activate")
        print(f"'{args.username}' {'activated' if cmd == 'activate' else 'deactivated; its tokens were revoked'}.")
    elif cmd == "token":
        token, record = svc.issue_token(_user_by_name(svc, args.username).id, "api", args.name,
                                        hours=args.days * 24 if args.days else None)
        print(f"API token for {args.username} ('{record.name}'"
              + (f", expires {record.expires_at[:10]}" if record.expires_at else ", does not expire") + "):")
        print(token)
        print("Store it now - it cannot be shown again. Send it as:  Authorization: Bearer <token>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="riskmesh", description="RiskMesh - quantitative risk management")
    p.add_argument("--config", metavar="FILE", help="riskmesh.toml to use (default: see `riskmesh config path`)")
    p.add_argument("--backend", choices=["json", "sql", "neo4j", "memory"],
                   help="overrides the configured backend")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--reason", help="why you make this change (recorded in the audit log)")
    p.add_argument("--user", help="name recorded in the audit log (default: [user] name or login name)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="replace all data with the example organisation")

    xl = sub.add_parser("excel", help="download or upload all data as an Excel workbook")
    xl_sub = xl.add_subparsers(dest="excel_command", required=True)
    xl_sub.add_parser("export", help="write all data (or an empty template) to a workbook").add_argument("file")
    xli = xl_sub.add_parser("import", help="read a workbook; shows what changes and asks first")
    xli.add_argument("file")
    xli.add_argument("--replace", action="store_true", help="delete stored rows that are missing from a sheet")
    xli.add_argument("--dry-run", action="store_true", help="only show what would change")
    xli.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    tp = sub.add_parser("third-party", help="suppliers, services and customers: concentration and findings")
    tp_sub = tp.add_subparsers(dest="tp_command", required=True)
    tp_sub.add_parser("overview", help="what depends on which supplier and service")
    tp_sub.add_parser("findings", help="what a NIS2/DORA review would ask about")
    tpe = tp_sub.add_parser("exposure", help="who is hit if a supplier or service fails")
    tpe.add_argument("name", help="supplier or service name (or id)")
    tpd = tp_sub.add_parser("dora", help="what a DORA customer's register of information needs from us")
    tpd.add_argument("customer", help="customer name")
    tpd.add_argument("--xlsx", metavar="FILE", help="also write it as an Excel workbook")
    tpc = tp_sub.add_parser("customer", help="what a customer depends on")
    tpc.add_argument("name", help="customer name (or id)")
    tps = tp_sub.add_parser("stress", help="what an outage of N hours costs (own cost and penalties)")
    tps.add_argument("name", help="supplier or service name (or id)")
    tps.add_argument("hours", type=float)
    sub.add_parser("tree", help="show the organisation hierarchy")
    sub.add_parser("portfolio", help="correlations between risks and the risk appetite")
    sub.add_parser("incidents", help="open incidents and the reporting / notification deadlines they start")
    calp = sub.add_parser("calibrate", help="what observed events and benchmarks say about the estimates")
    calp.add_argument("risk", nargs="?", help="risk name: show the details (and --apply)")
    calp.add_argument("--expert-years", type=float, default=2.0, help="weight of the estimate in years of data")
    calp.add_argument("--apply", action="store_true", help="replace the risk's estimate with the suggestion")
    sub.add_parser("risks", help="list risks with analytical expected annual loss")

    sim = sub.add_parser("simulate", help="run a Monte Carlo simulation")
    sim.add_argument("--engine", choices=["analytic", "monte_carlo"],
                     help="analytic: exact via FFT; monte_carlo: simulation (default: from config)")
    sim.add_argument("--trials", type=int, help="Monte Carlo only (default: from config)")
    sim.add_argument("--seed", type=int, help="Monte Carlo only (default: from config)")
    sim.add_argument("--inherent", action="store_true", help="ignore controls (residual specs)")
    sim.add_argument("--compare", action="store_true", help="show inherent and residual")
    sim.add_argument("--json", metavar="FILE", help="also write the full report to FILE")
    sim.add_argument("--label", help="name the run, e.g. 'Board meeting 2026-12-01'")
    sim.add_argument("--no-cache", action="store_true", help="recalculate even if a stored run matches")

    runs = sub.add_parser("runs", help="stored analysis runs")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_sub.add_parser("list", help="list runs, newest first")
    for name, text in (("show", "print a stored report"), ("reproduce", "recalculate and compare")):
        rp = runs_sub.add_parser(name, help=text)
        rp.add_argument("run", help="run number (e.g. 17) or id")
    lb = runs_sub.add_parser("label", help="name a run")
    lb.add_argument("run")
    lb.add_argument("label")

    bk = sub.add_parser("backup", help="complete backup incl. audit trail, runs and users (JSON)")
    bk.add_argument("file")
    rs = sub.add_parser("restore", help="restore a backup into empty storage and verify the audit chain")
    rs.add_argument("file")
    rs.add_argument("--force", action="store_true", help="delete existing data first")

    exp = sub.add_parser("export", help="export all data to a JSON file")
    exp.add_argument("file")
    imp = sub.add_parser("import", help="import a JSON export (replaces data)")
    imp.add_argument("file")
    imp.add_argument("--merge", action="store_true", help="keep existing data")

    aud = sub.add_parser("audit", help="show or verify the audit log")
    aud_sub = aud.add_subparsers(dest="audit_command", required=True)
    ver = aud_sub.add_parser("verify", help="check the hash chain and the data against it")
    ver.add_argument("--chain-only", action="store_true", help="do not compare the stored data")
    kg = aud_sub.add_parser("keygen", help="create an Ed25519 key for signing anchors")
    kg.add_argument("path", nargs="?", default="riskmesh-audit-key.pem")
    kg.add_argument("--force", action="store_true")
    an = aud_sub.add_parser("anchor", help="sign and/or time-stamp the current chain head")
    an.add_argument("--reason")
    aud_sub.add_parser("anchors", help="list anchors")
    log_ = aud_sub.add_parser("log", help="show the newest audit entries")
    log_.add_argument("--limit", type=int, default=20)
    log_.add_argument("--entity", help="history of one entity id")

    srv = sub.add_parser("serve", help="start the REST API")
    srv.add_argument("--host", help="default: [api] host")
    srv.add_argument("--port", type=int, help="default: [api] port")
    srv.add_argument("--reload", action="store_true")
    srv.add_argument("--workers", type=int, default=1, help="worker processes (needs the SQL or Neo4j backend)")

    sub.add_parser("gui", help="start the desktop application")

    per = sub.add_parser("period", help="close reporting periods and compare them")
    per_sub = per.add_subparsers(dest="period_command", required=True)
    pc = per_sub.add_parser("close", help="freeze the figures of a period: 2026, 2026-Q4 or 2026-11")
    pc.add_argument("key")
    pc.add_argument("--note")
    per_sub.add_parser("reopen", help="unfreeze a closed period").add_argument("key")
    per_sub.add_parser("list", help="closed periods")
    pcmp = per_sub.add_parser("compare", help="compare two closed periods")
    pcmp.add_argument("a")
    pcmp.add_argument("b")
    ptr = per_sub.add_parser("trend", help="a figure over all closed periods")
    ptr.add_argument("--metric", default="eal_residual")
    ptr.add_argument("--level", default="total", choices=["total", "unit", "category", "risk"])
    ptr.add_argument("--entity", help="unit id, category or risk id")
    per_sub.add_parser("export", help="all period facts as CSV").add_argument("file")

    usr = sub.add_parser("users", help="manage users who log in to the API / web app")
    usr_sub = usr.add_subparsers(dest="users_command", required=True)
    usr_sub.add_parser("list", help="list users")
    ca = usr_sub.add_parser("create-admin", help="create an administrator (asks for the password)")
    ca.add_argument("username")
    cu = usr_sub.add_parser("create", help="create a user (asks for the password)")
    cu.add_argument("username")
    cu.add_argument("--role", choices=["viewer", "editor", "admin"], default="viewer")
    cu.add_argument("--name", help="display name")
    cu.add_argument("--email")
    pw = usr_sub.add_parser("password", help="set a user's password (asks for it)")
    pw.add_argument("username")
    rl = usr_sub.add_parser("role", help="change a user's role")
    rl.add_argument("username")
    rl.add_argument("role", choices=["viewer", "editor", "admin"])
    for name, text in (("deactivate", "block a user (keeps the history)"), ("activate", "unblock a user")):
        usr_sub.add_parser(name, help=text).add_argument("username")
    tk = usr_sub.add_parser("token", help="create an API token for scripts (shown once)")
    tk.add_argument("username")
    tk.add_argument("--name", default="script")
    tk.add_argument("--days", type=int, help="expires after this many days (default: never)")

    cfg = sub.add_parser("config", help="create or show the configuration file")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    init = cfg_sub.add_parser("init", help="write a commented riskmesh.toml")
    init.add_argument("path", nargs="?", help="where to write it (default: ./riskmesh.toml)")
    init.add_argument("--user-dir", action="store_true", help="write it to your user config folder")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    cfg_sub.add_parser("show", help="print the effective settings (secrets masked)")
    cfg_sub.add_parser("path", help="print which configuration file is used")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "config" and args.config_command == "init":
        target = Path(args.path) if args.path else (
            user_config_dir() / FILE_NAME if args.user_dir else Path.cwd() / FILE_NAME)
        try:
            print(f"Wrote {write_template(target, overwrite=args.force)}")
        except FileExistsError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    try:
        settings = load_settings(args.config)
    except (OSError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if args.backend:
        settings.backend = args.backend

    if args.command == "config":
        if args.config_command == "path":
            print(settings.config_file or "No configuration file found; using defaults and environment variables.")
        else:
            print(f"# file: {settings.config_file or '(none - defaults)'}")
            for key, value in settings.public().items():
                if key != "config_file":
                    print(f"{key:<18} = {value}")
        return 0

    if args.command == "gui":
        from ..gui import main as gui_main
        return gui_main(settings)

    if args.command == "serve":
        import uvicorn
        if settings.config_file:
            os.environ["RISKMESH_CONFIG"] = settings.config_file
        os.environ["RISKMESH_BACKEND"] = settings.backend
        if args.workers > 1 and settings.backend in ("json", "memory"):
            print("Error: several workers need the sql or neo4j backend (json/memory are one process).",
                  file=sys.stderr)
            return 1
        uvicorn.run("riskmesh.presentation.api:create_app", factory=True, workers=args.workers,
                    host=args.host or settings.api_host, port=args.port or settings.api_port, reload=args.reload)
        return 0

    args.engine = getattr(args, "engine", None) or settings.engine
    args.trials = getattr(args, "trials", None) or settings.n_trials
    if getattr(args, "seed", None) is None:
        args.seed = settings.seed

    try:
        svc = make_service(settings, actor=args.user, source="cli")
        with svc.repo:
            svc.reason = args.reason
            return run(args, svc)
    except DomainError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run(args, svc: RiskService) -> int:
    if args.command == "demo":
        seed_demo(svc)
        c = svc.dashboard_summary()["counts"]
        print(f"Demo data loaded: {c['business_units']} business units, {c['assets']} assets, "
              f"{c['persons']} persons, {c['risks']} risks.")

    elif args.command == "tree":
        print_tree(svc.organisation_tree())

    elif args.command == "risks":
        units = {u.id: u.name for u in svc.list_business_units()}
        print(f"{'Risk':<28}{'Unit':<22}{'Category':<13}{'EAL (residual)':>15}")
        for r in svc.list_risks():
            eal = svc.risk_scores(r)["residual"]["expected_annual_loss"]  # incl. outage consequences
            print(f"{r.name[:27]:<28}{units.get(r.business_unit_id, '?')[:21]:<22}"
                  f"{(r.category or '')[:12]:<13}{eal:>15,.0f}")

    elif args.command == "simulate":
        if args.compare:
            result = svc.run_analysis("compare", args.engine, args.trials, args.seed, unit_id=None,
                                      use_cache=not args.no_cache, label=args.label)
            print_report(result["inherent"], "Inherent risk")
            print_report(result["residual"], "Residual risk (after controls)")
            print(f"\nControls reduce expected annual loss by {result['reduction_in_expected_loss']:,.0f}"
                  f" and P99 by {result['reduction_in_p99']:,.0f}")
        else:
            result = svc.run_analysis("single", args.engine, args.trials, args.seed, residual=not args.inherent,
                                      use_cache=not args.no_cache, label=args.label)
            print_report(result, "Inherent risk" if args.inherent else "Residual risk")
        run = result["run"]
        print(f"\nRun #{run['number']} ({'stored result reused' if run['cached'] else 'calculated now'}), "
              f"fingerprint {run['fingerprint'][:16]}" + (f", '{run['label']}'" if run.get("label") else ""))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            print(f"Report written to {args.json}")

    elif args.command == "export":
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(svc.export_data(), f, indent=2)
        print(f"Exported to {args.file}")

    elif args.command == "backup":
        data = svc.backup_data()
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"Backup written to {args.file}: audit head #{data['audit_head_seq']} {data['audit_head_hash'][:16]}…, "
              f"checksum {data['content_sha256'][:16]}…")

    elif args.command == "restore":
        with open(args.file, "r", encoding="utf-8") as f:
            result = svc.restore_data(json.load(f), force=args.force)
        print("Restored:", ", ".join(f"{v} {k}" for k, v in result["restored"].items() if v))
        v = result["verification"]
        print("Audit chain verified" if v["ok"] else f"VERIFICATION FAILED: {v['chain_errors'] + v['data_errors']}")
        return 0 if v["ok"] else 2

    elif args.command == "import":
        with open(args.file, "rb") as f:
            raw = f.read()
        svc.source = f"import:{os.path.basename(args.file)}:sha256:{hashlib.sha256(raw).hexdigest()}"
        counts = svc.import_data(json.loads(raw.decode("utf-8")), replace=not args.merge)
        print("Imported:", ", ".join(f"{v} {k}" for k, v in counts.items()))

    elif args.command == "users":
        return _users(args, svc)

    elif args.command == "period":
        return _period(args, svc)

    elif args.command == "calibrate":
        if not args.risk:
            print(f"{'Risk':<26}{'Events':>7}{'Years':>7}{'Estimate':>10}{'Data':>9}{'Suggested':>11}  Loss")
            for r in svc.calibration_overview(args.expert_years):
                obs = "-" if r["observed_rate"] is None else f"{r['observed_rate']:.2f}"
                loss = "" if r["impact_change"] is None else f"{r['impact_change']:+.0%} median"
                print(f"{r['name'][:25]:<26}{r['events']:>7}{r['years']:>7.1f}{r['current_rate']:>10.2f}{obs:>9}"
                      f"{r['calibrated_rate']:>11.2f}  {loss}")
            print("\nEvents per year. Details: riskmesh calibrate \"<risk>\"  (--apply to accept)")
            return 0
        risk = _find(svc.list_risks(), args.risk)
        if not risk:
            print(f"Error: no risk called {args.risk!r}", file=sys.stderr)
            return 1
        c = svc.calibrate_risk(risk.id, args.expert_years)
        f = c["frequency"]
        print(f"{risk.name}: evidence categories {', '.join(c['risk']['evidence_categories']) or '-'}")
        print(f"Data since {c['since'] or '-'} ({c['years']:g} years): {f['events']} events, "
              f"{len(c['benchmarks'])} benchmarks")
        print(f"{c['frequency_field']}: {f['current_rate']:.3f}/year -> suggested {f['calibrated_rate']:.3f}/year "
              f"(90% {f['interval'][0]:.3f}-{f['interval'][1]:.3f})")
        if c["impact"]:
            i = c["impact"]
            print(f"{c['impact_field']}: P10-P90 {i['current']['p10']:,.0f}-{i['current']['p90']:,.0f} -> "
                  f"{i['suggested']['p10']:,.0f}-{i['suggested']['p90']:,.0f} ({i['observed']['count']} observed losses)")
        else:
            print(c["impact_note"])
        if args.apply:
            svc.apply_calibration(risk.id, expert_years=args.expert_years)
            print("Applied (recorded in the audit log).")
    elif args.command == "incidents":
        o = svc.incident_overview()
        if not o["open"]:
            print("No open incidents.")
        for i in o["open"]:
            sig = {True: "NIS2 significant", False: "not significant", None: "NIS2 not assessed"}[i["nis2_significant"]]
            print(f"{i['detected_at']}  {i['title']}  [{i['status'].lower()}, {sig}, {i['customer_count']} customers]")
        if o["deadlines"]:
            print(f"\n{'Due':<18}{'Status':<10}{'Deadline':<40}Incident")
            for d in o["deadlines"]:
                print(f"{d['due']:<18}{d['status'].replace('_', ' '):<10}{d['label'][:39]:<40}{d['incident']}")
        if o["overdue"]:
            print(f"\n{o['overdue']} deadline(s) overdue.")
    elif args.command == "portfolio":
        names = {r.id: r.name for r in svc.list_risks()}
        units = {u.id: u.name for u in svc.list_business_units()}
        print(f"{'Correlation':<58}{'Coefficient':>12}")
        for c in svc.list_correlations():
            pair = f"{names.get(c.risk_a_id, c.risk_a_id)} ~ {names.get(c.risk_b_id, c.risk_b_id)}"
            print(f"{pair[:57]:<58}{c.coefficient:>12.2f}")
        for a in svc.list_appetites():
            print(f"\nAppetite: {a.name} ({units.get(a.business_unit_id, 'whole organisation')})")
            for pt in a.points:
                print(f"  at most {pt['probability']:.2%} chance per year of losing more than {money(pt['loss']).strip()}")
        print("\nSee 'riskmesh simulate' for whether the appetite is kept.")
    elif args.command == "third-party":
        return _third_party(args, svc)

    elif args.command == "excel":
        return _excel(args, svc)

    elif args.command == "runs":
        if args.runs_command == "list":
            for r in svc.list_runs():
                print(f"#{r['number']:<4} {r['created_at'][:19]}  {r['created_by']:<12} {r['mode']:<8} "
                      f"{r['engine']:<12} {'STALE' if r['stale'] else 'current':<8} {r.get('label') or ''}")
        else:
            run_id = _resolve_run(svc, args.run)
            if args.runs_command == "show":
                report = svc.get_run(run_id)
                if report["run"]["mode"] == "compare":
                    print_report(report["inherent"], "Inherent risk")
                    print_report(report["residual"], "Residual risk (after controls)")
                else:
                    print_report(report, "Report")
                print(f"\nRun #{report['run']['number']}" + ("  (data has changed since)" if report["run"]["stale"] else ""))
            elif args.runs_command == "reproduce":
                print(svc.reproduce_run(run_id)["message"])
            elif args.runs_command == "label":
                run = svc.label_run(run_id, args.label)
                print(f"Run #{run.number} labelled '{run.label}'")

    elif args.command == "audit" and args.audit_command == "keygen":
        from ..domain import anchoring
        try:
            path, kid = anchoring.generate_key(Path(args.path), overwrite=args.force)
        except FileExistsError as e:
            print(f"Error: {e} (use --force to replace it)", file=sys.stderr)
            return 1
        print(f"Created {path} (key id {kid}). Set [audit] signing_key in riskmesh.toml to use it.")
        print("Keep this file away from the database and note the key id somewhere else.")

    elif args.command == "audit" and args.audit_command == "anchor":
        from ..domain import anchoring
        a = svc.create_anchor(args.reason)
        print("Anchored", anchoring.describe(a.to_dict()))

    elif args.command == "audit" and args.audit_command == "anchors":
        from ..domain import anchoring
        for a in svc.list_anchors():
            print(f"{(a.created_at or '')[:19]}  {anchoring.describe(a.to_dict())}  {a.reason or ''}")

    elif args.command == "audit" and args.audit_command == "verify":
        result = svc.verify_audit(check_data=not args.chain_only)
        print(f"Entries: {result['entries']}   head: #{result['head_seq']} {result['head_hash']}")
        for err in result["chain_errors"]:
            print(f"  CHAIN  #{err['seq']}: {err['error']}")
        for err in result["data_errors"]:
            print(f"  DATA   {err['entity_type']} {err['entity_id']}: {err['error']}")
        for err in result["anchor_errors"]:
            print(f"  ANCHOR #{err['seq']}: {err['error']}")
        if result["anchors"]:
            print(f"Anchors: {len(result['anchors'])}, newest covers #{max(a['seq'] for a in result['anchors'])}")
        print("OK - audit log intact" if result["ok"] else "FAILED - see errors above")
        return 0 if result["ok"] else 2

    elif args.command == "audit" and args.audit_command == "log":
        for e in svc.list_audit(args.limit, entity_id=args.entity):
            print(f"#{e.seq:<5} {e.timestamp[:19]}  {e.user_id:<12} {e.action:<7} {e.entity_type:<13} "
                  f"{(e.details or '')[:30]:<30} [{e.source}]" + (f"  reason: {e.reason}" if e.reason else ""))
            for field, (old, new) in (e.changes or {}).items():
                if e.action == "UPDATE":
                    print(f"         {field}: {json.dumps(old)} -> {json.dumps(new)}")

    return 0
