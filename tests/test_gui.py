"""Smoke tests of the desktop GUI (offscreen). Skipped when PySide6 is not installed."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from riskmesh.config import Settings  # noqa: E402
from riskmesh.services.demo import seed_demo  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app):
    from riskmesh.gui.app import MainWindow
    w = MainWindow(Settings(backend="memory", user_name="tester"))
    seed_demo(w.svc)
    w.show()
    yield w
    w.close()


def test_every_page_renders_with_data(window):
    for i in range(len(window.PAGES)):
        window.nav.setCurrentRow(i)
        window.pages[i].refresh()
    dashboard, org, risks, controls = window.pages[:4]
    assert dashboard.top.rowCount() == 5 and dashboard.cards["risks"].value.text() == "7"
    assert org.tree.topLevelItemCount() == 1
    assert risks.table.rowCount() == 7 and controls.table.rowCount() == 5


def test_analysis_page_runs_and_reuses(window):
    page = window.pages[window.PAGES.index("Analysis")]
    page.run(use_cache=True)
    page.run(use_cache=True)
    assert page.stats.rowCount() == 5 and page.history.rowCount() == 1
    assert "stored result" in page.run_info.text()


def test_risk_dialog_round_trip(window):
    from riskmesh.gui.dialogs import RiskDialog
    svc = window.svc
    risk = svc.list_risks()[0]
    dialog = RiskDialog(svc, risk)
    values = dialog.values()
    assert values["frequency"] == risk.frequency and values["impact"] == risk.impact
    dialog.freq.value.setValue(0.25)
    assert "Inherent" in dialog.preview.text()
    with svc.context(reason="test"):
        svc.update_risk(risk.id, **dialog.values())
    entry = svc.list_audit(limit=1)[0]
    assert entry.source == "gui" and entry.user_id == "tester" and entry.reason == "test"
    assert entry.changes["frequency"][1]["rate"] == 0.25


def test_audit_page_verifies(window):
    page = window.pages[window.PAGES.index("Audit log")]
    page.refresh()
    page.verify()
    assert "intact" in page.status.text()


def test_viewer_cannot_edit_in_the_desktop_app(app):
    from riskmesh.gui.app import MainWindow
    from riskmesh.services import RiskService
    from riskmesh.storage import MemoryRepository
    svc = RiskService(MemoryRepository())
    seed_demo(svc)
    svc.create_user("admin", "admin", "correct horse battery")
    viewer = svc.create_user("vera", "viewer", "correct horse battery")
    w = MainWindow(Settings(backend="memory", auth_mode="local"), svc, viewer)
    assert svc.actor == "vera" and w.role == "viewer"
    risks = w.pages[w.PAGES.index("Risks")]
    assert risks.edit_buttons and not any(b.isEnabled() for b in risks.edit_buttons)
    assert not risks.can_edit and not any(a.isEnabled() for a in w.admin_actions)
    w.close()


def test_login_dialog(app):
    from riskmesh.gui.dialogs import LoginDialog
    from riskmesh.services import RiskService
    from riskmesh.storage import MemoryRepository
    svc = RiskService(MemoryRepository())
    svc.create_user("ed", "editor", "correct horse battery")
    d = LoginDialog(svc)
    d.username.setText("ed")
    d.password.setText("wrong password!")
    d.try_login()
    assert d.user is None and "Wrong" in d.message.text()
    d.password.setText("correct horse battery")
    d.try_login()
    assert d.user.username == "ed"


def test_reports_page_closes_and_compares(window):
    page = window.pages[window.PAGES.index("Reports")]
    window.svc.close_period("2026-Q3")
    window.svc.update_risk(window.svc.list_risks()[0].id, frequency={"type": "poisson", "rate": 4})
    window.svc.close_period("2026-Q4")
    page.refresh()
    assert page.periods.rowCount() == 2 and page.trend.rowCount() == 2
    page.compare()
    assert page.bridge.rowCount() == 6 and page.changes.rowCount() == 7


def test_third_party_page_shows_concentration(window):
    page = window.pages[window.PAGES.index("Third parties")]
    page.refresh()
    assert page.table.rowCount() == 3 and page.findings.rowCount() > 0
    page.table.selectRow(0)
    page.show_exposure()
    assert "If " in page.exposure.toPlainText()
    for tab in ("Services", "Customers"):
        page.kind.setCurrentText(tab)
        assert page.table.rowCount() == 3
        page.table.selectRow(0)
        page.show_exposure()
        assert page.exposure.toPlainText()
    from riskmesh.gui.dialogs import CustomerDialog, ServiceDialog, SupplierDialog
    c = window.svc.list_customers()[0]
    values = CustomerDialog(window.svc, c).values()
    assert values["service_ids"] == c.service_ids and values["regulations"] == c.regulations
    s = window.svc.list_services()[0]
    assert ServiceDialog(window.svc, s).values()["supplier_ids"] == s.supplier_ids
    sup = window.svc.list_suppliers()[0]
    assert SupplierDialog(window.svc, sup).values()["subcontractor_ids"] == sup.subcontractor_ids


def test_stress_test_and_outage_fields(window):
    page = window.pages[window.PAGES.index("Third parties")]
    page.refresh()
    page.table.selectRow(0)
    page.run_stress()
    assert "down for 24 hours" in page.exposure.toPlainText()
    from riskmesh.gui.dialogs import RiskDialog
    risk = next(r for r in window.svc.list_risks() if r.outage_hours)
    values = RiskDialog(window.svc, risk).values()
    assert values["outage_hours"] == risk.outage_hours and values["supplier_ids"] == risk.supplier_ids
    assert values["residual_outage_hours"] == risk.residual_outage_hours
    plain = next(r for r in window.svc.list_risks() if not r.outage_hours)
    assert RiskDialog(window.svc, plain).values()["outage_hours"] is None


def test_excel_menu_round_trip(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    path = str(tmp_path / "data.xlsx")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    window.export_excel()
    window.import_excel()
    assert shown and "matches the stored data" in shown[-1]


def test_portfolio_page_checks_appetite_and_lists_correlations(window):
    page = window.pages[window.PAGES.index("Appetite")]
    window.nav.setCurrentRow(window.PAGES.index("Appetite"))
    assert page.appetites.rowCount() == 2 and page.correlations.rowCount() == 3
    statuses = {page.appetites.item(r, 0).text(): page.appetites.item(r, 3).text() for r in range(2)}
    assert statuses == {"Group risk appetite": "within", "IT risk tolerance": "outside"}
    assert all(page.correlations.item(r, 3).text() for r in range(3))  # achieved loss correlation shown


def test_appetite_and_correlation_dialogs_round_trip(window):
    from riskmesh.gui.dialogs import AppetiteDialog, CorrelationDialog
    svc = window.svc
    a = svc.list_appetites()[0]
    v = AppetiteDialog(svc, a).values()
    assert v["points"] == a.points and v["business_unit_id"] == a.business_unit_id
    c = svc.list_correlations()[0]
    v = CorrelationDialog(svc, c).values()
    assert (v["risk_a_id"], v["risk_b_id"], v["coefficient"]) == (c.risk_a_id, c.risk_b_id, c.coefficient)


def test_analysis_page_shows_appetite_and_correlation_warning(window):
    page = window.pages[window.PAGES.index("Analysis")]
    page.engine.setCurrentIndex(page.engine.findData("analytic"))
    page.run(use_cache=True)
    assert "not applied" in page.appetite_info.text() and "IT risk tolerance" in page.appetite_info.text()
    page.engine.setCurrentIndex(page.engine.findData("monte_carlo"))
    page.trials.setValue(5_000)
    page.run(use_cache=True)
    assert "correlations applied" in page.appetite_info.text()
