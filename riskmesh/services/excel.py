"""
Excel workbook: download all data as one workbook, edit it, upload it again.

One sheet per entity type, written for people rather than machines:
references are names (a hidden ID column keeps the link stable), lists are
separated by ";", yes/no columns and fixed values get drop-down lists, and a
distribution is a model plus its few numbers. An empty download is the
template for a new organisation.

Upload is checked completely before anything is stored, and can be
previewed (`dry_run`): what would be created, updated, left alone or (with
replace) removed. Rows without an ID are new. Sheets missing from the
workbook are left untouched. With replace=False (merge, the default) rows
missing from a sheet are kept; with replace=True they are deleted.
Everything goes through import_data, so validation, cascades and the audit
log are exactly as for a JSON import.
"""

import io
import json
import re
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from ..domain import audit
from ..domain.errors import ValidationError
from ..domain.models import (
    CONTROL_EVENT_STATUSES,
    CONTROL_EVENT_TYPES,
    CONTROL_TYPES,
    DATA_TYPES,
    IMPLEMENTATION_STATUSES,
    INCIDENT_STATUSES,
    OBJECTIVE_CRITICALITIES,
    ORGANIZATION_TYPES,
    REGULATIONS,
    RISK_EVENT_STATUSES,
    RISK_EVENT_TYPES,
    SERVICE_CRITICALITIES,
    SUPPLIER_TIERS,
    Appetite,
    Asset,
    Benchmark,
    BusinessUnit,
    Control,
    ControlEvent,
    Correlation,
    Customer,
    Driver,
    DriverEffect,
    Entity,
    Incident,
    Objective,
    Observation,
    Person,
    Risk,
    RiskEvent,
    Scenario,
    Service,
    Supplier,
    new_id,
)
from ..domain.risk_model import negbin_from_range, rate_p90_of

SEP = "; "
MAX_ERRORS = 25

# distribution model names in the workbook <-> spec keys
MODELS = {"lognormal": "lognorm_p10_p90", "pert": "pert", "triangular": "triangular", "uniform": "uniform",
          "fixed": "fixed"}
MODEL_NAMES = {v: k for k, v in MODELS.items()}
SPEC_COLUMNS = [("low P10", "p10"), ("high P90", "p90"), ("min", "min"), ("most likely", "mode"),
                ("max", "max"), ("fixed", "value")]
SPEC_FIELDS = {"lognorm_p10_p90": ("p10", "p90"), "pert": ("min", "mode", "max"),
               "triangular": ("min", "mode", "max"), "uniform": ("min", "max"), "fixed": ("value",)}


class Col:
    """One column: header, model field, how to read/write it."""

    def __init__(self, header: str, field: str, kind: str = "text", required: bool = False,
                 values: Tuple[str, ...] = (), ref: Optional[Type[Entity]] = None, width: int = 18,
                 note: str = ""):
        self.header, self.field, self.kind, self.required = header, field, kind, required
        self.values, self.ref, self.width, self.note = values, ref, width, note


def _spec_cols(prefix: str, field: str) -> List[Col]:
    return ([Col(f"{prefix}: model", field, "spec_model", values=tuple(MODELS), width=14)]
            + [Col(f"{prefix}: {label}", field, "spec_value", width=12, note=key) for label, key in SPEC_COLUMNS])


SHEETS: List[Tuple[str, Type[Entity], List[Col]]] = [
    ("Organisation", BusinessUnit, [
        Col("Name", "name", required=True, width=28), Col("Code", "code", width=10),
        Col("Type", "organization_type", "enum", values=ORGANIZATION_TYPES, width=14),
        Col("Parent", "parent_id", "ref", ref=BusinessUnit, width=24),
        Col("Risk owner", "risk_owner_id", "ref", ref=Person, width=20),
        Col("Delegates", "delegate_ids", "refs", ref=Person, width=24),
        Col("LEI", "lei", width=22), Col("Country", "country", width=9),
        Col("Description", "description", width=40)]),
    ("People", Person, [
        Col("Name", "name", required=True, width=24), Col("Title", "title", width=20),
        Col("Email", "email", width=28)]),
    ("Assets", Asset, [
        Col("Name", "name", required=True, width=26), Col("Type", "asset_type", required=True, width=14),
        Col("Organisation", "business_unit_id", "ref", required=True, ref=BusinessUnit, width=22),
        Col("Criticality", "criticality", width=12), Col("Value", "value", "number", width=14),
        Col("Description", "description", width=40)]),
    ("Suppliers", Supplier, [
        Col("Name", "name", required=True, width=26),
        Col("Tier", "tier", "enum", values=SUPPLIER_TIERS, width=12), Col("Country", "country", width=10),
        Col("Relationship owner", "owner_id", "ref", ref=Person, width=20),
        Col("Contract start", "contract_start", "date", width=13), Col("Contract end", "contract_end", "date", width=13),
        Col("Next review", "next_review", "date", width=13),
        Col("Personal data", "processes_personal_data", "bool", width=10),
        Col("Data locations", "data_locations", "list", width=16),
        Col("Certifications", "certifications", "list", width=24),
        Col("Exit plan", "exit_plan", "bool", width=10),
        Col("Subcontractors", "subcontractor_ids", "refs", ref=Supplier, width=26),
        Col("LEI", "lei", width=22), Col("Company number", "company_number", width=14),
        Col("Ultimate parent", "parent_company", width=22),
        Col("Description", "description", width=40)]),
    ("Services", Service, [
        Col("Name", "name", required=True, width=28),
        Col("Delivered by", "business_unit_id", "ref", required=True, ref=BusinessUnit, width=22),
        Col("Criticality", "criticality", "enum", values=SERVICE_CRITICALITIES, width=12),
        Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("RTO hours", "rto_hours", "number", width=10), Col("RPO hours", "rpo_hours", "number", width=10),
        Col("Outage cost per hour", "outage_cost_per_hour", "number", width=14),
        Col("Suppliers", "supplier_ids", "refs", ref=Supplier, width=28),
        Col("Assets", "asset_ids", "refs", ref=Asset, width=24),
        Col("ICT service type", "ict_service_type", width=24),
        Col("Data locations", "data_locations", "list", width=16),
        Col("Description", "description", width=40)]),
    ("Customers", Customer, [
        Col("Name", "name", required=True, width=26), Col("Sector", "sector", width=12),
        Col("Regulation", "regulations", "list", values=REGULATIONS, width=24,
            note="; ".join(REGULATIONS)),
        Col("Critical function", "critical_function", "bool", width=10),
        Col("Services", "service_ids", "refs", ref=Service, width=30),
        Col("Account owner", "account_owner_id", "ref", ref=Person, width=20),
        Col("Contract start", "contract_start", "date", width=13), Col("Contract end", "contract_end", "date", width=13),
        Col("Contract value", "contract_value", "number", width=14),
        Col("Notify within hours", "notification_hours", "number", width=10),
        Col("Audit rights", "audit_rights", "bool", width=10), Col("Exit support", "exit_support", "bool", width=10),
        Col("Penalty per hour", "penalty_per_hour", "number", width=12),
        Col("Penalty after hours", "penalty_grace_hours", "number", width=10),
        Col("Penalty cap", "penalty_cap", "number", width=12),
        Col("Their own loss per outage hour", "own_loss_per_outage_hour", "number", width=14),
        Col("Notes", "notes", width=40)]),
    ("Objectives", Objective, [
        Col("Name", "name", required=True, width=34),
        Col("Criticality", "criticality", "enum", values=OBJECTIVE_CRITICALITIES, width=16),
        Col("Organisation", "business_unit_id", "ref", ref=BusinessUnit, width=22),
        Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("Delivered through services", "service_ids", "refs", ref=Service, width=28),
        Col("KPI", "kpi", width=30), Col("Target date", "target_date", "date", width=12),
        Col("Tolerance: loss above", "tolerance_loss", "number", width=14),
        Col("Tolerance: max chance per year %", "tolerance_probability", "percent", width=12),
        Col("Description", "description", width=40)]),
    ("Risks", Risk, [
        Col("Name", "name", required=True, width=28),
        Col("Organisation", "business_unit_id", "ref", required=True, ref=BusinessUnit, width=22),
        Col("Category", "category", width=12), Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("Events per year", "frequency", "freq_rate", width=10),
        Col("Events per year, high (P90)", "frequency", "freq_high", width=10),
        Col("Chance per year %", "frequency", "freq_p", width=10),
        *_spec_cols("Loss", "impact"),
        Col("Residual events per year", "residual_frequency", "freq_rate", width=10),
        Col("Residual events per year, high (P90)", "residual_frequency", "freq_high", width=10),
        Col("Residual chance per year %", "residual_frequency", "freq_p", width=10),
        *_spec_cols("Residual loss", "residual_impact"),
        *_spec_cols("Outage hours", "outage_hours"),
        *_spec_cols("Residual outage hours", "residual_outage_hours"),
        Col("Assets", "asset_ids", "refs", ref=Asset, width=22),
        Col("Services", "service_ids", "refs", ref=Service, width=26),
        Col("Suppliers", "supplier_ids", "refs", ref=Supplier, width=22),
        Col("Evidence categories", "evidence_categories", "list", width=24),
        Col("Threatens objectives", "objective_ids", "refs", ref=Objective, width=28),
        Col("Method", "method", width=14),
        Col("Method inputs (JSON)", "method_inputs", "json", width=40),
        Col("Description", "description", width=40)]),
    ("Drivers", Driver, [
        Col("Name", "name", required=True, width=30), Col("Level", "level", "number", width=8),
        Col("Bad-year multiplier", "uncertainty", "number", width=10),
        Col("Owner", "owner_id", "ref", ref=Person, width=20), Col("Indicator", "indicator", width=34),
        Col("Description", "description", width=40)]),
    ("Driver effects", DriverEffect, [
        Col("Driver", "driver_id", "ref", required=True, ref=Driver, width=28),
        Col("Risk", "risk_id", "ref", required=True, ref=Risk, width=28),
        Col("Frequency sensitivity", "frequency_sensitivity", "number", width=11),
        Col("Loss sensitivity", "impact_sensitivity", "number", width=11),
        Col("Rationale", "rationale", width=40)]),
    ("Scenarios", Scenario, [
        Col("Name", "name", required=True, width=30), Col("Likelihood", "likelihood", width=20),
        Col("Driver levels (driver=level; ...)", "driver_levels", "levels", ref=Driver, width=40),
        Col("Risks hit (risk: frequency x, impact y; ...)", "risk_factors", "factors", ref=Risk, width=48),
        Col("Outage of supplier", "outage_supplier_id", "ref", ref=Supplier, width=22),
        Col("Outage of service", "outage_service_id", "ref", ref=Service, width=22),
        Col("Outage hours", "outage_hours", "number", width=9),
        Col("Description", "description", width=40)]),
    ("Correlations", Correlation, [
        Col("Risk A", "risk_a_id", "ref", required=True, ref=Risk, width=28),
        Col("Risk B", "risk_b_id", "ref", required=True, ref=Risk, width=28),
        Col("Coefficient -1 to 1", "coefficient", "number", required=True, width=12),
        Col("Rationale", "rationale", width=50)]),
    ("Appetite", Appetite, [
        Col("Name", "name", required=True, width=28),
        Col("Organisation", "business_unit_id", "ref", ref=BusinessUnit, width=22),
        Col("Tolerance (loss @ max chance per year)", "points", "points", required=True, width=40),
        Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("Statement", "statement", width=50)]),
    ("Controls", Control, [
        Col("Name", "name", required=True, width=28),
        Col("Organisation", "business_unit_id", "ref", required=True, ref=BusinessUnit, width=22),
        Col("Type", "control_type", "enum", values=CONTROL_TYPES, width=13),
        Col("Implementation", "implementation_status", "enum", values=IMPLEMENTATION_STATUSES, width=14),
        Col("Effectiveness 1-5", "effectiveness_rating", "int", width=10),
        Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("Risks", "risk_ids", "refs", ref=Risk, width=30),
        Col("Implementation cost", "implementation_cost", "number", width=14),
        Col("Annual cost", "annual_cost", "number", width=12),
        Col("Lifetime years", "lifetime_years", "number", width=9),
        Col("Fewer events %", "frequency_reduction", "percent", width=10),
        Col("Lower loss per event %", "impact_reduction", "percent", width=10),
        Col("Improves driver", "driver_id", "ref", ref=Driver, width=24),
        Col("Lowers the driver %", "driver_reduction", "percent", width=10),
        Col("Description", "description", width=40)]),
    ("Risk events", RiskEvent, [
        Col("Name", "name", required=True, width=28),
        Col("Risk", "risk_id", "ref", required=True, ref=Risk, width=26),
        Col("Date", "date", "date", required=True, width=12),
        Col("Type", "event_type", "enum", values=RISK_EVENT_TYPES, width=14),
        Col("Status", "status", "enum", values=RISK_EVENT_STATUSES, width=14),
        Col("Financial impact", "financial_impact", "number", width=14),
        Col("Description", "description", width=40), Col("Impact description", "impact_description", width=30)]),
    ("Incidents", Incident, [
        Col("Title", "title", required=True, width=30),
        Col("Detected (YYYY-MM-DD HH:MM)", "detected_at", "time", required=True, width=17),
        Col("Status", "status", "enum", values=INCIDENT_STATUSES, width=12),
        Col("NIS2 significant", "nis2_significant", "maybe", width=11),
        Col("Services", "service_ids", "refs", ref=Service, width=26),
        Col("Started at supplier", "supplier_ids", "refs", ref=Supplier, width=22),
        Col("Risk", "risk_id", "ref", ref=Risk, width=22), Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("Resolved", "resolved_at", "time", width=17),
        Col("Financial impact", "financial_impact", "number", width=14),
        Col("Root cause", "root_cause", width=30), Col("Description", "description", width=40),
        Col("Notifications sent (JSON)", "notifications", "json", width=30)]),
    ("Observations", Observation, [
        Col("Date", "date", "date", required=True, width=12),
        Col("Category", "category", required=True, width=20),
        Col("Count", "count", "int", width=8), Col("Loss", "loss", "number", width=14),
        Col("Organisation", "business_unit_id", "ref", ref=BusinessUnit, width=22),
        Col("Supplier", "supplier_id", "ref", ref=Supplier, width=22),
        Col("Risk", "risk_id", "ref", ref=Risk, width=24),
        Col("Source", "source", width=20), Col("Description", "description", width=40)]),
    ("Benchmarks", Benchmark, [
        Col("Name", "name", required=True, width=32), Col("Category", "category", required=True, width=20),
        Col("Events per year", "events_per_year", "number", width=12),
        Col("Loss low P10", "loss_p10", "number", width=14), Col("Loss high P90", "loss_p90", "number", width=14),
        Col("Weight", "weight", "number", width=9), Col("Year", "year", "int", width=8),
        Col("Source", "source", width=36), Col("Notes", "notes", width=40)]),
    ("Control tests", ControlEvent, [
        Col("Control", "control_id", "ref", required=True, ref=Control, width=28),
        Col("Date", "date", "date", required=True, width=12),
        Col("Type", "event_type", "enum", values=CONTROL_EVENT_TYPES, width=12),
        Col("Status", "status", "enum", values=CONTROL_EVENT_STATUSES, width=12),
        Col("Description", "description", width=40)]),
]
SHEET_OF = {cls: name for name, cls, _ in SHEETS}

README = [
    "RiskMesh workbook",
    "",
    "One sheet per kind of data. Edit, add or delete rows, then upload the workbook in RiskMesh",
    "(web: Import / Export, desktop: File > Import from Excel, command line: riskmesh excel import).",
    "",
    "- ID: leave empty for new rows; keep it for existing ones (it is what links a row to stored data).",
    "- References (Organisation, Owner, Risk, ...) are names; use the ID instead if a name is not unique.",
    "- Lists (Delegates, Suppliers, Services, ...) are separated by ';'.",
    "- Yes/no columns: yes or no.  Dates: YYYY-MM-DD.",
    "- Frequency: fill either 'Events per year' (can repeat) or 'Chance per year %' (at most once).",
    "    'Events per year, high (P90)' makes the rate itself uncertain (e.g. typically 2, up to 4).",
    "- A distribution is a model (lognormal, pert, triangular, uniform, fixed) and its numbers:",
    "    lognormal: low P10 and high P90 (80% of events between them); pert/triangular: min, most likely, max;",
    "    uniform: min and max; fixed: fixed. Leave the model empty for 'none' (e.g. no residual estimate).",
    "- Outage hours: how long one event takes the linked services down (costs and customer penalties per hour).",
    "- Incidents: times as YYYY-MM-DD HH:MM; NIS2 significant yes, no or empty (not yet assessed).",
    "- Risks: Method 'quantitative' uses the columns above; 'iso27005' or 'fair' take their inputs from the",
    "    Method inputs column (JSON, e.g. {\"likelihood\": 4, \"consequence\": 3}) and fill in the distributions.",
    "- Scenarios: drivers at other levels ('Economic climate=1.5'), risks hit harder ('Ransomware attack:",
    "    frequency 3, impact 1.5'; separate several with ';') and/or an outage of a supplier or service.",
    "- Drivers: common causes. Level 1 = as estimated; Bad-year multiplier e.g. 1.5. Driver effects: how",
    "    strongly each risk follows its driver (sensitivity 1 = proportionally, 0.5 weaker, 2 stronger).",
    "- Controls: costs and effect (Fewer events %, Lower loss per event %) value the control (Decisions).",
    "- Objectives: what we must achieve. Risks threaten one when they name it (Threatens objectives) or can",
    "    disrupt a service it is delivered through. Tolerance: at most this chance per year of losing more.",
    "- Observations: events that happened, one row per event or a count (Count) with their total Loss.",
    "    Fill Organisation or Supplier for that level; leave both empty for the whole group.",
    "    They count for risks whose Evidence categories (or category) include the observation's Category.",
    "- Benchmarks: public figures for organisations like ours (events per year, loss range per event).",
    "    Weight: how much it counts - in years of our own data (frequency) and in events (loss).",
    "- Correlations: how strongly two risks tend to have bad years together, -1 to 1 (0.3 weak, 0.6 strong).",
    "- Appetite tolerance: points 'loss @ chance', e.g. '1000000 @ 10%; 10000000 @ 1%' = at most 10% chance",
    "    per year of losing more than 1,000,000 and at most 1% of more than 10,000,000. No organisation = all.",
    "",
    "Upload shows a preview first. Sheets you remove from the workbook are left untouched.",
    "Merge (default) keeps stored rows that are not in a sheet; replace deletes them (administrators only).",
]


# -- cell conversion -------------------------------------------------------------------------------

def _text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _number(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise ValueError("expected a number")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if "," in s and "." in s:          # 1.234,5 or 1,234.5
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", s) and len(set(re.findall(r"[.,]", s))) == 1:
        s = re.sub(r"[.,]", "", s)     # 10.000 / 1,000,000: thousands separators
    else:
        s = s.replace(",", ".")        # 2,5 -> 2.5
    return float(s)


def _bool(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("yes", "y", "ja", "j", "true", "x", "1", "1.0"):
        return True
    if s in ("no", "n", "nej", "false", "0", "0.0", ""):
        return False
    raise ValueError("expected yes or no")


def _date(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()[:10]
    try:
        date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"'{v}' is not a date (YYYY-MM-DD)") from None
    return s


def _points(v: Any) -> List[Dict[str, float]]:
    """'1000000 @ 10%; 10000000 @ 1%' -> [{"loss": 1e6, "probability": 0.1}, ...]."""
    out = []
    for part in _split(v):
        if "@" not in part:
            raise ValueError(f"'{part}' should be 'loss @ chance', e.g. '1000000 @ 10%'")
        loss_text, prob_text = part.split("@", 1)
        prob_text = prob_text.strip()
        percent = prob_text.endswith("%")
        loss, prob = _number(loss_text), _number(prob_text.rstrip("%"))
        if loss is None or prob is None:
            raise ValueError(f"'{part}' should be 'loss @ chance', e.g. '1000000 @ 10%'")
        out.append({"loss": loss, "probability": prob / 100 if percent or prob >= 1 else prob})
    return out


def _split(v: Any, comma: bool = False) -> List[str]:
    s = _text(v)
    if not s:
        return []
    for sep in ("\n", ",") if comma else ("\n",):
        s = s.replace(sep, ";")
    return [x.strip() for x in s.split(";") if x.strip()]


class ExcelExchange:
    """Mixin for RiskService."""

    # -- export ------------------------------------------------------------------------------------

    def _customer_part(self, customer_id: str) -> Dict[type, set]:
        """The ids of everything that belongs to one customer's part of the graph."""
        p = self.customer_profile(customer_id)
        services = {s["id"] for s in p["services"]}
        suppliers = {s["id"] for s in p["suppliers"]}
        risks = {r["id"] for r in p["risks"]}
        controls = {c["id"] for c in p["controls"]}
        g = self._graph()
        incidents = {i.id for i in self.repo.list(Incident)
                     if customer_id in {c.id for c in self._affected(i, g)["customers"].values()}}
        return {
            Customer: {customer_id}, Service: services, Supplier: suppliers, Risk: risks, Control: controls,
            Incident: incidents,
            RiskEvent: {e.id for e in self.repo.list(RiskEvent) if e.risk_id in risks},
            ControlEvent: {e.id for e in self.repo.list(ControlEvent) if e.control_id in controls},
            Objective: {o.id for o in self.repo.list(Objective) if set(o.service_ids) & services},
            DriverEffect: {e.id for e in self.repo.list(DriverEffect) if e.risk_id in risks},
            Driver: {e.driver_id for e in self.repo.list(DriverEffect) if e.risk_id in risks},
            Correlation: {c.id for c in self.repo.list(Correlation) if c.risk_a_id in risks and c.risk_b_id in risks},
            Observation: {o.id for o in self.repo.list(Observation)
                          if o.risk_id in risks or (o.supplier_id in suppliers)},
            Asset: {a for s in self.repo.list(Service) if s.id in services for a in s.asset_ids}
            | {a for r in self.repo.list(Risk) if r.id in risks for a in r.asset_ids},
            Appetite: set(), Scenario: set(),
        }

    def export_excel(self, customer_id: Optional[str] = None) -> bytes:
        """
        All data as a workbook; with customer_id only that customer's part (its services, the
        suppliers behind them, the risks to them and what belongs to those), plus the people,
        organisation units and benchmarks the rows refer to.
        """
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation

        data = {cls: sorted(self.repo.list(cls), key=lambda e: (getattr(e, "name", "") or "", e.id))
                for cls in DATA_TYPES}
        if customer_id:
            keep = self._customer_part(customer_id)
            data = {cls: [e for e in items if cls not in keep or e.id in keep[cls]] for cls, items in data.items()}
        names = self._names(data)

        wb = Workbook()
        ws = wb.active
        ws.title = "Read me"
        for line in README:
            ws.append([line])
        ws["A1"].font = Font(bold=True, size=14)
        ws.column_dimensions["A"].width = 110

        lists = wb.create_sheet("Lists")  # names for the drop-downs of single references
        lists.sheet_state = "hidden"
        list_ranges: Dict[type, str] = {}
        for i, cls in enumerate((BusinessUnit, Person, Risk, Control, Supplier, Service, Asset), start=1):
            values = sorted({names[cls][e.id] for e in data[cls]})
            letter = get_column_letter(i)
            lists.cell(row=1, column=i, value=cls.__name__)
            for r, v in enumerate(values, start=2):
                lists.cell(row=r, column=i, value=v)
            if values:
                list_ranges[cls] = f"Lists!${letter}$2:${letter}${len(values) + 1}"

        head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1E3A5F")
        for title, cls, cols in SHEETS:
            ws = wb.create_sheet(title)
            ws.append(["ID"] + [c.header + (" *" if c.required else "") for c in cols])
            for e in data[cls]:
                ws.append([e.id] + [self._cell(e, c, names) for c in cols])
            for i, c in enumerate(["ID"] + cols, start=1):
                cell = ws.cell(row=1, column=i)
                cell.font, cell.fill = head_font, head_fill
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                ws.column_dimensions[get_column_letter(i)].width = 10 if i == 1 else c.width
            ws.row_dimensions[1].height = 32
            ws.freeze_panes = "C2"
            ws.auto_filter.ref = ws.dimensions
            last = max(ws.max_row, 2) + 500
            for i, c in enumerate(cols, start=2):
                letter = get_column_letter(i)
                formula = None
                if c.kind in ("enum", "spec_model"):
                    formula = '"' + ",".join(c.values) + '"'
                elif c.kind == "bool":
                    formula = '"yes,no"'
                elif c.kind == "ref" and c.ref in list_ranges:
                    formula = list_ranges[c.ref]
                if formula:
                    dv = DataValidation(type="list", formula1=formula, allow_blank=True,
                                        showErrorMessage=c.kind != "ref")
                    if c.kind == "ref":
                        dv.errorStyle = "information"  # an ID is fine too
                    ws.add_data_validation(dv)
                    dv.add(f"{letter}2:{letter}{last}")
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    @staticmethod
    def _names(data: Dict[type, List[Entity]]) -> Dict[type, Dict[str, str]]:
        """id -> the text written for a reference: the name, or the id where the name is not unique."""
        out: Dict[type, Dict[str, str]] = {}
        for cls, items in data.items():
            label = ((lambda e: f"{e.date} {e.id[:8]}") if cls is ControlEvent
                     else (lambda e: getattr(e, "name", None) or getattr(e, "title", None) or e.id))
            counts: Dict[str, int] = {}
            for e in items:
                counts[label(e)] = counts.get(label(e), 0) + 1
            out[cls] = {e.id: (label(e) if counts[label(e)] == 1 else e.id) for e in items}
        return out

    @staticmethod
    def _cell(e: Entity, c: Col, names: Dict[type, Dict[str, str]]) -> Any:
        v = getattr(e, c.field)
        if c.kind == "ref":
            return names[c.ref].get(v, v) if v else None
        if c.kind == "refs":
            # only rows that are in the workbook (a customer's part leaves the rest out)
            return SEP.join(names[c.ref][x] for x in v if x in names[c.ref]) or None
        if c.kind == "list":
            return SEP.join(v) or None
        if c.kind == "bool":
            return "yes" if v else "no"
        if c.kind == "freq_rate":
            return v["rate"] if v and v.get("type") in ("poisson", "negbin") else None
        if c.kind == "freq_high":
            return round(rate_p90_of(v), 4) if v and v.get("type") == "negbin" else None
        if c.kind == "freq_p":
            return round(float(v["p"]) * 100, 6) if v and v.get("type") == "bernoulli" else None
        if c.kind == "spec_model":
            if not v:
                return None
            return MODEL_NAMES.get(v.get("dist")) or json.dumps(v)  # custom distributions keep their JSON
        if c.kind == "spec_value":
            return v.get(c.note) if v and v.get("dist") in SPEC_FIELDS else None
        if c.kind == "maybe":
            return None if v is None else ("yes" if v else "no")
        if c.kind == "percent":
            return None if v is None else round(v * 100, 6)
        if c.kind == "json":
            return json.dumps(v, ensure_ascii=False) if v else None
        if c.kind == "time":
            return v.replace("T", " ") if v else None
        if c.kind == "levels":
            return SEP.join(f"{names[c.ref].get(k, k)}={x:g}" for k, x in (v or {}).items()) or None
        if c.kind == "factors":
            return SEP.join(f"{names[c.ref].get(k, k)}: " + ", ".join(f"{key} {x:g}" for key, x in f.items())
                            for k, f in (v or {}).items()) or None
        if c.kind == "points":
            return SEP.join(f"{p['loss']:g} @ {p['probability'] * 100:g}%" for p in v or []) or None
        return v

    # -- import ------------------------------------------------------------------------------------

    def import_excel(self, content: bytes, replace: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        """
        Reads a workbook made by export_excel (or written by hand in the same layout).
        Returns {"changes": {kind: {created, updated, unchanged, removed}}, "sheets": [...], "applied": bool}.
        Raises ValidationError listing the problems (sheet, row, column) if anything is wrong.
        """
        data, sheets = self._read_workbook(content, replace)
        changes = self._changes(data, replace)
        if dry_run:
            # the whole import, validation included, on a copy: nothing is stored
            from ..storage import MemoryRepository
            scratch = type(self)(MemoryRepository())
            scratch.import_data(self.export_data(), replace=True)
            scratch.import_data(data, replace=replace)
        else:
            self.import_data(data, replace=replace)
        return {"changes": changes, "sheets": sheets, "replace": replace, "applied": not dry_run}

    def _read_workbook(self, content: bytes, replace: bool) -> Tuple[Dict[str, Any], List[str]]:
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        except Exception as e:  # zipfile.BadZipFile, KeyError, InvalidFileException, ...
            raise ValidationError(f"Not an Excel workbook (.xlsx): {e}") from e

        errors: List[str] = []
        rows_by_cls: Dict[type, List[Tuple[int, Dict[str, Any]]]] = {}
        present: List[str] = []
        for title, cls, cols in SHEETS:
            if title not in wb.sheetnames:
                continue
            present.append(title)
            ws = wb[title]
            it = ws.iter_rows(values_only=True)
            header = next(it, None) or ()
            index = {}
            for i, h in enumerate(header):
                key = (_text(h) or "").rstrip("*").strip().lower()
                index[key] = i
            if "id" not in index:
                errors.append(f"{title}: the first row must contain the column headers (ID, ...).")
                continue
            missing = [c.header for c in cols if c.required and c.header.lower() not in index]
            if missing:
                errors.append(f"{title}: missing column(s) {', '.join(missing)}.")
                continue
            rows = []
            for r, values in enumerate(it, start=2):
                if not values or all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
                    continue
                cells = {h: (values[i] if i < len(values) else None) for h, i in index.items()}
                rows.append((r, cells))
            rows_by_cls[cls] = rows
        if errors:
            raise ValidationError("The workbook could not be read:\n" + "\n".join(errors))

        stored = {cls: {e.id: e for e in self.repo.list(cls)} for cls in DATA_TYPES}
        ids: Dict[type, Dict[int, str]] = {}
        for cls, rows in rows_by_cls.items():
            ids[cls] = {r: (_text(cells.get("id")) or new_id()) for r, cells in rows}
            dupes = [i for i in set(ids[cls].values()) if list(ids[cls].values()).count(i) > 1]
            if dupes:
                errors.append(f"{SHEET_OF[cls]}: the same ID appears twice ({dupes[0]}).")

        # names that references can use: the workbook's rows, and stored entities the workbook keeps
        lookup: Dict[type, Dict[str, List[str]]] = {cls: {} for cls in DATA_TYPES}
        for cls in DATA_TYPES:
            known: Dict[str, str] = {}
            if cls in rows_by_cls:
                name_col = "name" if cls is not ControlEvent else None
                for r, cells in rows_by_cls[cls]:
                    known[ids[cls][r]] = _text(cells.get(name_col)) if name_col else None
                if not replace:
                    for e in stored[cls].values():
                        known.setdefault(e.id, getattr(e, "name", None))
            else:
                known = {e.id: getattr(e, "name", None) for e in stored[cls].values()}
            for i, name in known.items():
                lookup[cls].setdefault(i, [i])
                if name:
                    lookup[cls].setdefault(name.lower(), []).append(i)

        def resolve(cls: type, text: str) -> str:
            hits = lookup[cls].get(text) or lookup[cls].get(text.lower()) or []
            hits = list(dict.fromkeys(hits))
            if not hits:
                raise ValueError(f"no {SHEET_OF[cls].lower().rstrip('s')} called '{text}'")
            if len(hits) > 1:
                raise ValueError(f"'{text}' is not unique; use the ID")
            return hits[0]

        out: Dict[str, Any] = {}
        for title, cls, cols in SHEETS:
            if cls not in rows_by_cls:
                continue
            items = []
            for r, cells in rows_by_cls[cls]:
                eid = ids[cls][r]
                base = stored[cls][eid].to_dict() if eid in stored[cls] else {}
                d = dict(base, id=eid)
                specs: Dict[str, Dict[str, Any]] = {}
                failed = set()
                for c in cols:
                    raw = cells.get(c.header.lower())
                    if c.header.lower() not in cells:
                        continue  # column not in this workbook: keep the stored value
                    try:
                        self._read_cell(d, specs, c, raw, resolve)
                    except (ValueError, TypeError) as e:
                        errors.append(f"{title} row {r}, {c.header}: {e}")
                        failed.add(c.field)
                for field, spec in specs.items():
                    try:
                        d[field] = self._finish_spec(spec)
                    except ValueError as e:
                        errors.append(f"{title} row {r}, {field.replace('_', ' ')}: {e}")
                for c in cols:
                    if c.required and c.field not in failed and d.get(c.field) in (None, "", []):
                        errors.append(f"{title} row {r}: {c.header} is required.")
                items.append(d)
            out[cls.kind + "s"] = items
            if len(errors) > MAX_ERRORS:
                break
        if errors:
            more = f"\n... and {len(errors) - MAX_ERRORS} more" if len(errors) > MAX_ERRORS else ""
            raise ValidationError("The workbook has errors:\n" + "\n".join(errors[:MAX_ERRORS]) + more)
        if replace:  # sheets that are not in the workbook are left as they are
            for cls in DATA_TYPES:
                if cls not in rows_by_cls:
                    out[cls.kind + "s"] = [e.to_dict() for e in stored[cls].values()]
        return out, present

    @staticmethod
    def _read_cell(d: Dict[str, Any], specs: Dict[str, Dict[str, Any]], c: Col, raw: Any,
                   resolve: Callable[[type, str], str]) -> None:
        if c.kind == "freq_high":
            v = _number(raw)
            if v is not None:
                specs.setdefault(c.field, {})["_high"] = v
            return
        if c.kind in ("freq_rate", "freq_p"):
            v = _number(raw)
            spec = specs.setdefault(c.field, {})
            spec["_freq"] = True
            if v is not None:
                if "type" in spec:
                    raise ValueError("fill either events per year or chance per year, not both")
                spec.update({"type": "poisson", "rate": v} if c.kind == "freq_rate"
                            else {"type": "bernoulli", "p": v / 100})
            return
        if c.kind == "spec_model":
            specs.setdefault(c.field, {})["_model"] = _text(raw)
            return
        if c.kind == "spec_value":
            v = _number(raw)
            if v is not None:
                specs.setdefault(c.field, {})[c.note] = v
            return
        if c.kind == "ref":
            t = _text(raw)
            d[c.field] = resolve(c.ref, t) if t else None
        elif c.kind == "refs":
            d[c.field] = [resolve(c.ref, t) for t in _split(raw)]
        elif c.kind == "list":
            values = _split(raw, comma=True)
            if c.values:
                values = [v.upper().replace(" ", "_") for v in values]
                bad = [v for v in values if v not in c.values]
                if bad:
                    raise ValueError(f"unknown value(s) {', '.join(bad)}; use {', '.join(c.values)}")
            d[c.field] = values
        elif c.kind == "enum":
            t = _text(raw)
            if t is None:
                return  # empty: the stored value (or for a new row the default) applies
            t = t.upper().replace(" ", "_")
            if t not in c.values:
                raise ValueError(f"'{raw}' is not one of {', '.join(c.values)}")
            d[c.field] = t
        elif c.kind == "bool":
            d[c.field] = _bool(raw)
        elif c.kind == "number":
            v = _number(raw)
            if v is None and c.field in ("weight", "lifetime_years", "level", "uncertainty", "frequency_sensitivity",
                                         "impact_sensitivity"):
                return  # empty: the stored value or the default (1)
            d[c.field] = v
        elif c.kind == "int":
            v = _number(raw)
            if v is not None and not float(v).is_integer():
                raise ValueError("expected a whole number")
            if v is None and c.field in ("count",):
                return  # empty: the stored value or the default (1)
            d[c.field] = int(v) if v is not None else None
        elif c.kind == "date":
            d[c.field] = _date(raw)
        elif c.kind == "points":
            d[c.field] = _points(raw)
        elif c.kind == "levels":
            out = {}
            for part in _split(raw):
                name, _, value = part.rpartition("=")
                if not name or _number(value) is None:
                    raise ValueError(f"'{part}' should be 'driver=level', e.g. 'Economic climate=1.5'")
                out[resolve(c.ref, name.strip())] = _number(value)
            d[c.field] = out
        elif c.kind == "factors":
            out = {}
            for part in _split(raw):
                name, _, rest = part.partition(":")
                f = {}
                for item in rest.split(","):
                    key, _, value = item.strip().partition(" ")
                    if key.lower() not in ("frequency", "impact") or _number(value) is None:
                        raise ValueError(f"'{part}' should be 'risk: frequency 2, impact 1.5'")
                    f[key.lower()] = _number(value)
                out[resolve(c.ref, name.strip())] = f
            d[c.field] = out
        elif c.kind == "percent":
            v = _number(str(raw).replace("%", "") if isinstance(raw, str) else raw)
            d[c.field] = None if v is None else v / 100
        elif c.kind == "maybe":
            t = _text(raw)
            d[c.field] = None if t is None else _bool(t)
        elif c.kind == "json":
            t = _text(raw)
            try:
                d[c.field] = json.loads(t) if t else {}
            except json.JSONDecodeError:
                raise ValueError("not valid JSON; leave it empty or as downloaded") from None
        elif c.kind == "time":
            if isinstance(raw, datetime):
                d[c.field] = raw.strftime("%Y-%m-%dT%H:%M")
            else:
                t = _text(raw)
                d[c.field] = t.replace(" ", "T") if t else None
        else:
            d[c.field] = _text(raw)

    @staticmethod
    def _finish_spec(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        high = spec.pop("_high", None)
        if spec.pop("_freq", False):
            if high is not None:
                if spec.get("type") != "poisson":
                    raise ValueError("the high estimate needs 'events per year'")
                if high < spec["rate"]:
                    raise ValueError("the high estimate is below the events per year")
                return negbin_from_range(spec["rate"], high)
            return spec or None
        model = spec.pop("_model", None)
        if not model:
            if spec:
                raise ValueError("numbers given but no model; choose lognormal, pert, triangular, uniform or fixed")
            return None
        if model.startswith("{"):
            return json.loads(model)  # a custom distribution written as JSON
        dist = MODELS.get(model.lower())
        if not dist:
            raise ValueError(f"unknown model '{model}'; use {', '.join(MODELS)}")
        needed = SPEC_FIELDS[dist]
        missing = [k for k in needed if k not in spec]
        if missing:
            labels = dict((key, label) for label, key in SPEC_COLUMNS)
            raise ValueError(f"{model} needs {', '.join(labels[k] for k in missing)}")
        return {"dist": dist, **{k: spec[k] for k in needed}}

    def _changes(self, data: Dict[str, Any], replace: bool) -> Dict[str, Dict[str, int]]:
        out = {}
        for cls in DATA_TYPES:
            key = cls.kind + "s"
            if key not in data:
                continue
            stored = {e.id: e for e in self.repo.list(cls)}
            c = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0}
            incoming = set()
            for d in data[key]:
                incoming.add(d["id"])
                old = stored.get(d["id"])
                if old is None:
                    c["created"] += 1
                else:
                    new = cls.from_dict({**d, "created_at": old.created_at, "updated_at": old.updated_at})
                    c["updated" if audit.diff(old, new) else "unchanged"] += 1
            if replace:
                c["removed"] = len(set(stored) - incoming)
            out[cls.kind] = c
        return out
