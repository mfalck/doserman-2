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
import hashlib
import json
import os
from pathlib import Path
import logging
import sys
from typing import Any, Dict, List

from ..bootstrap import make_service
from ..config import FILE_NAME, load_settings, user_config_dir, write_template
from ..domain import risk_model
from ..domain.errors import DomainError, NotFoundError
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
    sub.add_parser("tree", help="show the organisation hierarchy")
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

    exp = sub.add_parser("export", help="export all data to a JSON file")
    exp.add_argument("file")
    imp = sub.add_parser("import", help="import a JSON export (replaces data)")
    imp.add_argument("file")
    imp.add_argument("--merge", action="store_true", help="keep existing data")

    aud = sub.add_parser("audit", help="show or verify the audit log")
    aud_sub = aud.add_subparsers(dest="audit_command", required=True)
    ver = aud_sub.add_parser("verify", help="check the hash chain and the data against it")
    ver.add_argument("--chain-only", action="store_true", help="do not compare the stored data")
    log_ = aud_sub.add_parser("log", help="show the newest audit entries")
    log_.add_argument("--limit", type=int, default=20)
    log_.add_argument("--entity", help="history of one entity id")

    srv = sub.add_parser("serve", help="start the REST API")
    srv.add_argument("--host", help="default: [api] host")
    srv.add_argument("--port", type=int, help="default: [api] port")
    srv.add_argument("--reload", action="store_true")

    sub.add_parser("gui", help="start the desktop application")

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
        uvicorn.run("riskmesh.presentation.api:create_app", factory=True,
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
            eal = risk_model.expected_annual_loss(r.residual_frequency or r.frequency,
                                                  r.residual_impact or r.impact)
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

    elif args.command == "import":
        with open(args.file, "rb") as f:
            raw = f.read()
        svc.source = f"import:{os.path.basename(args.file)}:sha256:{hashlib.sha256(raw).hexdigest()}"
        counts = svc.import_data(json.loads(raw.decode("utf-8")), replace=not args.merge)
        print("Imported:", ", ".join(f"{v} {k}" for k, v in counts.items()))

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

    elif args.command == "audit" and args.audit_command == "verify":
        result = svc.verify_audit(check_data=not args.chain_only)
        print(f"Entries: {result['entries']}   head: #{result['head_seq']} {result['head_hash']}")
        for err in result["chain_errors"]:
            print(f"  CHAIN  #{err['seq']}: {err['error']}")
        for err in result["data_errors"]:
            print(f"  DATA   {err['entity_type']} {err['entity_id']}: {err['error']}")
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
