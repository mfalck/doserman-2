"""Main window of the RiskMesh desktop application."""

import hashlib
import json
import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from .. import __version__
from ..bootstrap import make_service
from ..config import Settings, load_settings
from ..services.demo import seed_demo
from .dialogs import LoginDialog
from .pages import (
    AnalysisPage,
    AuditPage,
    ControlsPage,
    DashboardPage,
    DecisionsPage,
    DriversPage,
    EvidencePage,
    IncidentsPage,
    ObjectivesPage,
    OrganisationPage,
    PortfolioPage,
    ReportsPage,
    RisksPage,
    ThirdPartyPage,
)
from .widgets import guarded


class MainWindow(QMainWindow):
    PAGES = ["Overview", "Organisation", "Objectives", "Risks", "Controls", "Incidents", "Data", "Third parties",
             "Drivers", "Appetite", "Analysis", "Decisions", "Reports", "Audit log"]

    def __init__(self, settings: Settings, svc=None, user=None):
        super().__init__()
        self.settings = settings
        self.svc = svc or make_service(settings, source="gui")
        self.user = user
        if user is not None:
            self.svc.actor = user.username
        self.role = user.role if user is not None else "admin"
        self.setWindowTitle(f"RiskMesh {__version__}")
        self.resize(1280, 820)

        central = QWidget()
        layout = QHBoxLayout(central)
        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        font = QFont()
        font.setPointSize(11)
        self.nav.setFont(font)
        self.nav.addItems(self.PAGES)
        self.nav.setStyleSheet("QListWidget { border: none; background: #f8fafc; } "
                               "QListWidget::item { padding: 8px; } "
                               "QListWidget::item:selected { background: #dbeafe; color: black; }")
        self.stack = QStackedWidget()
        defaults = {"engine": settings.engine, "n_trials": settings.n_trials, "seed": settings.seed}
        self.pages = [DashboardPage(self.svc, self.refresh_all), OrganisationPage(self.svc, self.refresh_all),
                      ObjectivesPage(self.svc, self.refresh_all),
                      RisksPage(self.svc, self.refresh_all), ControlsPage(self.svc, self.refresh_all),
                      IncidentsPage(self.svc, self.refresh_all), EvidencePage(self.svc, self.refresh_all),
                      ThirdPartyPage(self.svc, self.refresh_all), DriversPage(self.svc, self.refresh_all),
                      PortfolioPage(self.svc, self.refresh_all),
                      AnalysisPage(self.svc, self.refresh_all, defaults), DecisionsPage(self.svc, self.refresh_all),
                      ReportsPage(self.svc, self.refresh_all),
                      AuditPage(self.svc, self.refresh_all)]
        for p in self.pages:
            self.stack.addWidget(p)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.currentRowChanged.connect(self._show_page)
        self._build_menu()
        self._build_status()
        self._apply_role()
        self.nav.setCurrentRow(0)

    # -- navigation / refresh ------------------------------------------------

    def _show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        guarded(self, self.pages[index].refresh)

    def refresh_all(self) -> None:
        """After a change: refresh the visible page now; others refresh when opened."""
        guarded(self, self.stack.currentWidget().refresh)

    # -- menus ---------------------------------------------------------------------

    def _build_menu(self) -> None:
        self.admin_actions = []
        self.editor_actions = []
        file_menu = self.menuBar().addMenu("&File")
        for text, slot, shortcut in (("Import from Excel…", self.import_excel, None),
                                     ("Export to Excel…", self.export_excel, None),
                                     (None, None, None),
                                     ("Import data (JSON)…", self.import_json, "Ctrl+I"),
                                     ("Export data (JSON)…", self.export_json, "Ctrl+E"),
                                     (None, None, None),
                                     ("Load demo data…", self.load_demo, None),
                                     (None, None, None),
                                     ("Open configuration file…", self.open_config, None),
                                     ("Show configuration", self.show_config, None),
                                     (None, None, None),
                                     ("Exit", self.close, "Ctrl+Q")):
            if text is None:
                file_menu.addSeparator()
                continue
            action = QAction(text, self)
            action.triggered.connect(slot)
            if shortcut:
                action.setShortcut(shortcut)
            if slot in (self.import_json, self.load_demo):
                self.admin_actions.append(action)
            if slot == self.import_excel:
                self.editor_actions.append(action)
            file_menu.addAction(action)
        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About RiskMesh", self)
        about.triggered.connect(lambda: QMessageBox.about(
            self, "RiskMesh", f"<b>RiskMesh {__version__}</b><br>Open source quantitative enterprise risk "
            "management.<br>Apache License 2.0<br><a href='https://github.com/mfalck/riskmesh'>"
            "github.com/mfalck/riskmesh</a>"))
        help_menu.addAction(about)

    def _build_status(self) -> None:
        s = self.settings
        where = s.json_path if s.backend == "json" else s.sql_url if s.backend == "sql" else \
            s.neo4j_uri if s.backend == "neo4j" else "in memory (not saved)"
        cfg = os.path.basename(s.config_file) if s.config_file else "no config file (defaults)"
        role = f" ({self.role})" if self.user is not None else ""
        label = QLabel(f"Backend: <b>{s.backend}</b> – {where} &nbsp;|&nbsp; Config: {cfg} &nbsp;|&nbsp; "
                       f"User: <b>{self.svc.actor}</b>{role}")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.statusBar().addWidget(label)

    # -- actions -------------------------------------------------------------------

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import RiskMesh data", "", "JSON (*.json)")
        if not path:
            return
        replace = QMessageBox.question(
            self, "Import", "Replace ALL existing data with the file?\n\nYes = replace, No = merge by id",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.No)
        if replace == QMessageBox.Cancel:
            return

        def do_import():
            with open(path, "rb") as f:
                raw = f.read()
            source = f"import:{os.path.basename(path)}:sha256:{hashlib.sha256(raw).hexdigest()}"
            with self.svc.context(source=source):
                return self.svc.import_data(json.loads(raw.decode("utf-8")), replace=replace == QMessageBox.Yes)
        counts = guarded(self, do_import)
        if counts:
            QMessageBox.information(self, "Import", "Imported: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
            self.refresh_all()

    def export_excel(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export to Excel", "riskmesh.xlsx", "Excel workbook (*.xlsx)")
        if path:
            def do_export():
                with open(path, "wb") as f:
                    f.write(self.svc.export_excel())
                return True
            if guarded(self, do_export):
                self.statusBar().showMessage(f"Workbook written to {path}", 5000)

    def import_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import from Excel", "", "Excel workbook (*.xlsx)")
        if not path:
            return
        replace = False
        if self.role == "admin":
            answer = QMessageBox.question(
                self, "Import from Excel", "Delete stored rows that are missing from a sheet (replace)?\n\n"
                "No = merge: add and update rows, keep everything else.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.No)
            if answer == QMessageBox.Cancel:
                return
            replace = answer == QMessageBox.Yes
        with open(path, "rb") as f:
            content = f.read()
        preview = guarded(self, lambda: self.svc.import_excel(content, replace=replace, dry_run=True))
        if not preview:
            return
        rows = [f"<tr><td>{k.replace('_', ' ')}</td><td align=right>{c['created']}</td>"
                f"<td align=right>{c['updated']}</td><td align=right>{c['unchanged']}</td>"
                f"<td align=right>{c['removed']}</td></tr>" for k, c in preview["changes"].items()]
        total = sum(c["created"] + c["updated"] + c["removed"] for c in preview["changes"].values())
        table = ("<table cellspacing=6><tr><th align=left>Sheet</th><th>New</th><th>Changed</th><th>Same</th>"
                 "<th>Removed</th></tr>" + "".join(rows) + "</table>")
        if not total:
            QMessageBox.information(self, "Import from Excel", "The workbook matches the stored data." + table)
            return
        if QMessageBox.question(self, "Import from Excel", f"Store these {total} changes?" + table) != QMessageBox.Yes:
            return
        source = f"excel:{os.path.basename(path)}:sha256:{hashlib.sha256(content).hexdigest()}"

        def do_import():
            with self.svc.context(source=source):
                return self.svc.import_excel(content, replace=replace)
        if guarded(self, do_import):
            self.statusBar().showMessage("Workbook imported (recorded in the audit log)", 5000)
            self.refresh_all()

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export RiskMesh data", "riskmesh-export.json", "JSON (*.json)")
        if path:
            def do_export():
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.svc.export_data(), f, indent=2, ensure_ascii=False)
                return True
            if guarded(self, do_export):
                self.statusBar().showMessage(f"Exported to {path}", 5000)

    def load_demo(self) -> None:
        if QMessageBox.warning(self, "Load demo data", "This deletes ALL data, including the audit log, and loads "
                               "the example organisation. Continue?",
                               QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            guarded(self, lambda: seed_demo(self.svc))
            self.refresh_all()

    def _apply_role(self) -> None:
        """Viewers see everything but cannot change it; data-wide actions need admin."""
        can_edit = self.role in ("editor", "admin")
        for page in self.pages:
            page.can_edit = can_edit
            for b in page.edit_buttons:
                b.setEnabled(can_edit)
        for action in self.admin_actions:
            action.setEnabled(self.role == "admin")
        for action in self.editor_actions:
            action.setEnabled(can_edit)
        for page in self.pages:
            for b in getattr(page, "admin_buttons", []):
                b.setEnabled(self.role == "admin")

    def open_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open riskmesh.toml", "", "RiskMesh config (*.toml)")
        if not path:
            return
        settings = guarded(self, lambda: load_settings(path))
        if settings:
            new = open_window(settings, self)
            if new:
                new.show()
                self._replacement = new  # keep a reference
                self.close()

    def show_config(self) -> None:
        lines = [f"<tr><td>{k}</td><td><b>{v}</b></td></tr>" for k, v in self.settings.public().items()]
        QMessageBox.information(self, "Configuration", "<table>" + "".join(lines) + "</table>")

    def closeEvent(self, event) -> None:
        self.svc.repo.close()
        super().closeEvent(event)


def open_window(settings: Settings, parent=None) -> Optional[MainWindow]:
    """
    Opens the service and, when login is enabled, asks for a local RiskMesh
    account first. Note: the desktop app works directly on the database, so
    the login decides who is recorded in the audit log and what the window
    allows - it does not protect a database file the user can open anyway.
    Returns None if the user cancels.
    """
    svc = make_service(settings, source="gui")
    user = None
    if settings.auth_mode != "none":
        if not [u for u in svc.list_users() if u.auth_source == "local" and u.active]:
            QMessageBox.warning(parent, "RiskMesh", "Login is enabled, but there is no local RiskMesh account.\n\n"
                                "Create one with:  riskmesh users create-admin <name>\n"
                                "(Single sign-on accounts can only be used in the web app.)")
            svc.repo.close()
            return None
        dialog = LoginDialog(svc, parent)
        if not dialog.exec():
            svc.repo.close()
            return None
        user = dialog.user
    return MainWindow(settings, svc, user)


def create_window(settings: Optional[Settings] = None) -> MainWindow:
    return MainWindow(settings or load_settings())
