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
from ..domain.models import (CONTROL_EVENT_STATUSES, CONTROL_EVENT_TYPES, CONTROL_TYPES, DATA_TYPES,
                             IMPLEMENTATION_STATUSES, ORGANIZATION_TYPES, REGULATIONS, RISK_EVENT_STATUSES,
                             RISK_EVENT_TYPES, SERVICE_CRITICALITIES, SUPPLIER_TIERS, Asset, BusinessUnit, Control,
                             ControlEvent, Customer, Entity, Person, Risk, RiskEvent, Service, Supplier, new_id)

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
        Col("Notes", "notes", width=40)]),
    ("Risks", Risk, [
        Col("Name", "name", required=True, width=28),
        Col("Organisation", "business_unit_id", "ref", required=True, ref=BusinessUnit, width=22),
        Col("Category", "category", width=12), Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("Events per year", "frequency", "freq_rate", width=10),
        Col("Chance per year %", "frequency", "freq_p", width=10),
        *_spec_cols("Loss", "impact"),
        Col("Residual events per year", "residual_frequency", "freq_rate", width=10),
        Col("Residual chance per year %", "residual_frequency", "freq_p", width=10),
        *_spec_cols("Residual loss", "residual_impact"),
        *_spec_cols("Outage hours", "outage_hours"),
        *_spec_cols("Residual outage hours", "residual_outage_hours"),
        Col("Assets", "asset_ids", "refs", ref=Asset, width=22),
        Col("Services", "service_ids", "refs", ref=Service, width=26),
        Col("Suppliers", "supplier_ids", "refs", ref=Supplier, width=22),
        Col("Description", "description", width=40)]),
    ("Controls", Control, [
        Col("Name", "name", required=True, width=28),
        Col("Organisation", "business_unit_id", "ref", required=True, ref=BusinessUnit, width=22),
        Col("Type", "control_type", "enum", values=CONTROL_TYPES, width=13),
        Col("Implementation", "implementation_status", "enum", values=IMPLEMENTATION_STATUSES, width=14),
        Col("Effectiveness 1-5", "effectiveness_rating", "int", width=10),
        Col("Owner", "owner_id", "ref", ref=Person, width=20),
        Col("Risks", "risk_ids", "refs", ref=Risk, width=30),
        Col("Description", "description", width=40)]),
    ("Risk events", RiskEvent, [
        Col("Name", "name", required=True, width=28),
        Col("Risk", "risk_id", "ref", required=True, ref=Risk, width=26),
        Col("Date", "date", "date", required=True, width=12),
        Col("Type", "event_type", "enum", values=RISK_EVENT_TYPES, width=14),
        Col("Status", "status", "enum", values=RISK_EVENT_STATUSES, width=14),
        Col("Financial impact", "financial_impact", "number", width=14),
        Col("Description", "description", width=40), Col("Impact description", "impact_description", width=30)]),
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
    "- A distribution is a model (lognormal, pert, triangular, uniform, fixed) and its numbers:",
    "    lognormal: low P10 and high P90 (80% of events between them); pert/triangular: min, most likely, max;",
    "    uniform: min and max; fixed: fixed. Leave the model empty for 'none' (e.g. no residual estimate).",
    "- Outage hours: how long one event takes the linked services down (costs and customer penalties per hour).",
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

    def export_excel(self) -> bytes:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation

        data = {cls: sorted(self.repo.list(cls), key=lambda e: (getattr(e, "name", "") or "", e.id))
                for cls in DATA_TYPES}
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
            label = (lambda e: f"{e.date} {e.id[:8]}") if cls is ControlEvent else (lambda e: e.name)
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
            return SEP.join(names[c.ref].get(x, x) for x in v) or None
        if c.kind == "list":
            return SEP.join(v) or None
        if c.kind == "bool":
            return "yes" if v else "no"
        if c.kind == "freq_rate":
            return v["rate"] if v and v.get("type") == "poisson" else None
        if c.kind == "freq_p":
            return round(float(v["p"]) * 100, 6) if v and v.get("type") == "bernoulli" else None
        if c.kind == "spec_model":
            if not v:
                return None
            return MODEL_NAMES.get(v.get("dist")) or json.dumps(v)  # custom distributions keep their JSON
        if c.kind == "spec_value":
            return v.get(c.note) if v and v.get("dist") in SPEC_FIELDS else None
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
            d[c.field] = _number(raw)
        elif c.kind == "int":
            v = _number(raw)
            if v is not None and not float(v).is_integer():
                raise ValueError("expected a whole number")
            d[c.field] = int(v) if v is not None else None
        elif c.kind == "date":
            d[c.field] = _date(raw)
        else:
            d[c.field] = _text(raw)

    @staticmethod
    def _finish_spec(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if spec.pop("_freq", False):
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
