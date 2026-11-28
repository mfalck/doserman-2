"""Excel workbook: download, edit, upload (all backends for the service, memory for the API)."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from riskmesh.config import Settings
from riskmesh.domain.errors import ValidationError
from riskmesh.presentation.api import create_app
from riskmesh.services import RiskService
from riskmesh.services.demo import seed_demo
from riskmesh.storage import MemoryRepository


def _strip(data):
    return {k: sorted(({f: v for f, v in e.items() if f not in ("created_at", "updated_at")} for e in items),
                      key=lambda e: e["id"])
            for k, items in data.items() if k != "version"}


def _edit(content: bytes, fn) -> bytes:
    wb = load_workbook(io.BytesIO(content))
    fn(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _row(ws, header: str, value):
    col = [c.value for c in ws[1]].index(header) + 1
    return next(r for r in range(2, ws.max_row + 1) if ws.cell(r, col).value == value)


def _set(ws, row: int, header: str, value):
    ws.cell(row, [c.value for c in ws[1]].index(header) + 1).value = value


def test_round_trip_changes_nothing(svc):
    seed_demo(svc)
    before = _strip(svc.export_data())
    content = svc.export_excel()
    preview = svc.import_excel(content, dry_run=True)
    assert all(c["created"] == c["updated"] == c["removed"] == 0 for c in preview["changes"].values())
    svc.import_excel(content)
    assert _strip(svc.export_data()) == before
    empty = RiskService(MemoryRepository())
    empty.import_excel(content)
    assert _strip(empty.export_data()) == before


def test_edit_add_and_reference_by_name(svc):
    seed_demo(svc)

    def edit(wb):
        ws = wb["Risks"]
        r = _row(ws, "Name *", "Payment fraud")
        _set(ws, r, "Events per year", 0.8)
        new = ws.max_row + 1
        for header, value in (("Name *", "Supplier insolvency"), ("Organisation *", "Finance"),
                              ("Chance per year %", 2), ("Loss: model", "pert"), ("Loss: min", "10.000"),
                              ("Loss: most likely", 50000), ("Loss: max", 400000),
                              ("Suppliers", "Nordic Cloud A/S; SecureMail ApS")):
            _set(ws, new, header, value)
        _set(wb["Customers"], _row(wb["Customers"], "Name *", "East Savings Bank"), "Exit support", "yes")

    content = _edit(svc.export_excel(), edit)
    preview = svc.import_excel(content, dry_run=True)
    assert preview["changes"]["risk"] == {"created": 1, "updated": 1, "unchanged": 6, "removed": 0}
    assert preview["changes"]["customer"]["updated"] == 1
    assert not any(r.name == "Supplier insolvency" for r in svc.list_risks())  # dry run stored nothing
    svc.import_excel(content)
    new = next(r for r in svc.list_risks() if r.name == "Supplier insolvency")
    assert new.frequency == {"type": "bernoulli", "p": 0.02}
    assert new.impact == {"dist": "pert", "min": 10_000, "mode": 50_000, "max": 400_000}
    assert len(new.supplier_ids) == 2
    assert next(r for r in svc.list_risks() if r.name == "Payment fraud").frequency["rate"] == 0.8
    assert svc.verify_audit()["ok"]


def test_errors_name_sheet_row_and_column(svc):
    seed_demo(svc)

    def edit(wb):
        ws = wb["Risks"]
        r = _row(ws, "Name *", "Payment fraud")
        _set(ws, r, "Organisation *", "Nowhere")
        _set(ws, r, "Loss: model", "lognormal")
        _set(ws, r, "Loss: low P10", None)
        ws2 = wb["Customers"]
        _set(ws2, _row(ws2, "Name *", "East Savings Bank"), "Regulation", "DORA; NIS3")

    with pytest.raises(ValidationError) as e:
        svc.import_excel(_edit(svc.export_excel(), edit))
    text = str(e.value)
    assert "Risks row" in text and "Organisation" in text and "Nowhere" in text
    assert "needs low P10" in text and "NIS3" in text


def test_replace_removes_missing_rows_and_keeps_missing_sheets(svc):
    seed_demo(svc)

    def edit(wb):
        ws = wb["Risk events"]
        ws.delete_rows(2, 1)
        del wb["Controls"]

    content = _edit(svc.export_excel(), edit)
    merge = svc.import_excel(content, dry_run=True)
    assert merge["changes"]["risk_event"]["removed"] == 0 and "control" not in merge["changes"]
    replace = svc.import_excel(content, replace=True)
    assert replace["changes"]["risk_event"]["removed"] == 1
    assert len(svc.list_risk_events()) == 2 and len(svc.list_controls()) == 10


def test_not_a_workbook_and_hand_made_sheet():
    svc = RiskService(MemoryRepository())
    with pytest.raises(ValidationError):
        svc.import_excel(b"not excel")
    wb = Workbook()
    ws = wb.active
    ws.title = "Organisation"
    ws.append(["ID", "Name", "Parent"])
    ws.append([None, "Group", None])
    ws.append([None, "IT", "Group"])
    buf = io.BytesIO()
    wb.save(buf)
    svc.import_excel(buf.getvalue())
    it = next(u for u in svc.list_business_units() if u.name == "IT")
    assert svc.get_business_unit(it.parent_id).name == "Group"


def test_api_download_preview_and_roles():
    svc = RiskService(MemoryRepository())
    seed_demo(svc)
    with TestClient(create_app(svc, Settings(backend="memory", api_key=None))) as c:
        r = c.get("/api/export.xlsx")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxml")
        preview = c.post("/api/import.xlsx?dry_run=true", content=r.content).json()
        assert preview["applied"] is False and preview["changes"]["risk"]["unchanged"] == 7
        assert c.post("/api/import.xlsx", content=b"").status_code == 422
        assert c.post("/api/import.xlsx", content=b"junk").status_code == 422
    from riskmesh.presentation.api import required_role
    assert required_role("POST", "/api/import.xlsx", {}) == "editor"
    assert required_role("POST", "/api/import.xlsx", {"replace": "true"}) == "admin"
