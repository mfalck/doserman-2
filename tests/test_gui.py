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
