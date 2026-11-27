"""The pages of the main window. Each has refresh(); the window calls it after any change."""

import functools
import json
from typing import Any, Callable, Dict, Optional

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QLogValueAxis, QValueAxis
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..services import RiskService
from .dialogs import ControlDialog, RiskDialog, UnitDialog
from .widgets import (LEVEL_COLORS, Card, Table, compact, describe_frequency, describe_impact, guarded, heading,
                      money, title)


class Page(QWidget):
    def __init__(self, svc: RiskService, on_change: Callable[[], None]):
        super().__init__()
        self.svc = svc
        self.on_change = on_change  # tell the window that data changed (refreshes all pages)
        self.layout_ = QVBoxLayout(self)
        self.can_edit = True        # set by the window from the signed-in user's role
        self.edit_buttons: list = []

    def refresh(self) -> None:
        raise NotImplementedError

    def changed(self) -> None:
        self.on_change()


def unit_names(svc: RiskService) -> Dict[str, str]:
    return {u.id: u.name for u in svc.list_business_units()}


def unit_filter_combo(svc: RiskService, combo: QComboBox) -> None:
    current = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("All organisations", None)
    for u in svc.list_business_units():
        combo.addItem(u.name, u.id)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    combo.blockSignals(False)


# ---------------------------------------------------------------------------

class DashboardPage(Page):
    def __init__(self, svc, on_change):
        super().__init__(svc, on_change)
        self.layout_.addWidget(heading("Overview"))
        grid = QGridLayout()
        self.cards = {key: Card(caption) for key, caption in (
            ("risks", "Risks"), ("high", "High priority risks"), ("score", "Average risk score"),
            ("eal", "Expected annual loss"), ("eff", "Control effectiveness"), ("events", "Open risk events"))}
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, i // 3, i % 3)
        self.layout_.addLayout(grid)
        row = QHBoxLayout()
        left, right = QVBoxLayout(), QVBoxLayout()
        left.addWidget(QLabel("<b>Top risks by expected loss (after controls)</b>"))
        self.top = Table(["Risk", "Expected loss / year"], numeric=[1])
        left.addWidget(self.top)
        right.addWidget(QLabel("<b>Risks by severity (inherent)</b>"))
        self.severity = Table(["Level", "Risks"], numeric=[1])
        right.addWidget(self.severity)
        row.addLayout(left, 3)
        row.addLayout(right, 2)
        self.layout_.addLayout(row)

    def refresh(self) -> None:
        d = self.svc.dashboard_summary()
        c = d["counts"]
        self.cards["risks"].set(str(c["risks"]), f"{c['risks_without_controls']} without controls")
        self.cards["high"].set(str(c["high_priority_risks"]), "inherent score 9 or higher")
        self.cards["score"].set(f"{d['average_inherent_score']:.1f} → {d['average_residual_score']:.1f}",
                                "inherent → residual (1-25)")
        self.cards["eal"].set(compact(d["expected_annual_loss_total"]), "sum over all risks after controls")
        eff = d["control_effectiveness"]
        self.cards["eff"].set("–" if eff is None else f"{eff:.0f}%",
                              f"{c['controls_implemented']} of {c['controls']} controls implemented")
        self.cards["events"].set(str(c["open_risk_events"]),
                                 f"{c['units_without_risk_owner']} of {c['business_units']} units lack a risk owner")
        self.top.fill([[r["name"], money(r["expected_annual_loss"])] for r in d["top_risks"]])
        sev = d["risks_by_severity"]
        self.severity.fill([[s["level"], s["count"]] for s in sev],
                           colors={(i, 0): LEVEL_COLORS[s["level"]] for i, s in enumerate(sev)})


# ---------------------------------------------------------------------------

class OrganisationPage(Page):
    def __init__(self, svc, on_change):
        super().__init__(svc, on_change)
        self.layout_.addWidget(heading("Organisation"))
        buttons = QHBoxLayout()
        for text, slot in (("New unit", self.add), ("New sub-unit", self.add_child), ("Edit", self.edit),
                           ("Delete", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            self.edit_buttons.append(b)
            buttons.addWidget(b)
        buttons.addStretch()
        self.layout_.addLayout(buttons)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Unit", "Code", "Type", "Risks", "Avg score", "Expected loss / year"])
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit())
        self.layout_.addWidget(self.tree)

    def refresh(self) -> None:
        summary = {u["id"]: u for u in self.svc.unit_risk_summary()}
        self.tree.clear()

        def add(nodes, parent):
            for n in nodes:
                s = summary.get(n["id"], {})
                item = QTreeWidgetItem([n["name"], n.get("code") or "", title(n["organization_type"]),
                                        str(s.get("risk_count", 0)),
                                        f"{s.get('average_inherent_score', 0):.1f}",
                                        money(s.get("expected_annual_loss", 0))])
                item.setData(0, Qt.UserRole, n["id"])
                for col in (3, 4, 5):
                    item.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
                (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
                add(n["children"], item)

        add(self.svc.organisation_tree(), None)
        self.tree.expandAll()
        for col in range(6):
            self.tree.resizeColumnToContents(col)

    def _selected(self) -> Optional[str]:
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _open(self, unit=None, parent_id=None):
        dialog = UnitDialog(self.svc, unit, parent_id, self)
        if dialog.exec():
            v = dialog.values()
            with self.svc.context(reason=dialog.change_reason()):
                if unit:
                    result = guarded(self, lambda: self.svc.update_business_unit(unit.id, **v))
                else:
                    owner = v.pop("risk_owner_id")

                    def create():
                        bu = self.svc.create_business_unit(v.pop("name"), v.pop("description"), v.pop("parent_id"), **v)
                        if owner:
                            self.svc.assign_risk_owner(bu.id, owner)
                        return bu
                    result = guarded(self, create)
            if result:
                self.changed()

    def add(self):
        self._open()

    def add_child(self):
        self._open(parent_id=self._selected())

    def edit(self):
        uid = self._selected()
        if uid:
            self._open(self.svc.get_business_unit(uid))

    def delete(self):
        uid = self._selected()
        if not uid:
            return
        unit = self.svc.get_business_unit(uid)
        reason, ok = QInputDialog.getText(
            self, "Delete unit", f"Delete '{unit.name}' and everything below it (sub-units, assets, risks, "
            "controls, events)?\n\nReason (recorded in the audit log):")
        if ok:
            with self.svc.context(reason=reason or None):
                if guarded(self, lambda: self.svc.delete_business_unit(uid, cascade=True)) is not None:
                    self.changed()


# ---------------------------------------------------------------------------

class RisksPage(Page):
    def __init__(self, svc, on_change):
        super().__init__(svc, on_change)
        self.layout_.addWidget(heading("Risks"))
        bar = QHBoxLayout()
        self.unit = QComboBox()
        self.unit.currentIndexChanged.connect(self.refresh)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search risks…")
        self.search.textChanged.connect(self.refresh)
        bar.addWidget(self.unit)
        bar.addWidget(self.search, 1)
        for text, slot in (("New risk", self.add), ("Edit", self.edit), ("Delete", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            self.edit_buttons.append(b)
            bar.addWidget(b)
        self.layout_.addLayout(bar)
        self.table = Table(["Risk", "Organisation", "Type", "Frequency", "Loss per event", "Inherent",
                            "Residual", "Expected loss / year"], numeric=[7])
        self.table.doubleClicked.connect(lambda *_: self.edit())
        self.layout_.addWidget(self.table)

    def refresh(self) -> None:
        unit_filter_combo(self.svc, self.unit)
        names = unit_names(self.svc)
        q = self.search.text().lower()
        risks = [r for r in self.svc.list_risk_views(unit_id=self.unit.currentData(), include_children=True)
                 if q in (r["name"] + (r.get("description") or "") + names.get(r["business_unit_id"], "")).lower()]
        risks.sort(key=lambda r: r["scores"]["inherent"]["score"], reverse=True)
        rows, colors = [], {}
        for i, r in enumerate(risks):
            inh, res = r["scores"]["inherent"], r["scores"]["residual"]
            rows.append([r["name"], names.get(r["business_unit_id"], "?"), r.get("category") or "",
                         describe_frequency(r["frequency"]), describe_impact(r["impact"]),
                         f"{inh['score']} ({inh['likelihood']}×{inh['impact']})",
                         f"{res['score']} ({res['likelihood']}×{res['impact']})",
                         money(res["expected_annual_loss"])])
            colors[(i, 5)] = LEVEL_COLORS[inh["level"]]
            colors[(i, 6)] = LEVEL_COLORS[res["level"]]
        self.table.fill(rows, keys=[r["id"] for r in risks], colors=colors)

    def add(self):
        dialog = RiskDialog(self.svc, unit_id=self.unit.currentData(), parent=self)
        if dialog.exec():
            v = dialog.values()
            if guarded(self, lambda: self.svc.create_risk(v.pop("business_unit_id"), v.pop("name"),
                                                          v.pop("frequency"), v.pop("impact"), **v)):
                self.changed()

    def edit(self):
        rid = self.table.selected_key()
        if not rid:
            return
        dialog = RiskDialog(self.svc, self.svc.get_risk(rid), parent=self)
        if dialog.exec():
            with self.svc.context(reason=dialog.change_reason()):
                if guarded(self, lambda: self.svc.update_risk(rid, **dialog.values())):
                    self.changed()

    def delete(self):
        rid = self.table.selected_key()
        if not rid:
            return
        risk = self.svc.get_risk(rid)
        reason, ok = QInputDialog.getText(self, "Delete risk", f"Delete '{risk.name}' and its risk events?\n\n"
                                          "Reason (recorded in the audit log):")
        if ok:
            with self.svc.context(reason=reason or None):
                if guarded(self, lambda: self.svc.delete_risk(rid)) is not None:
                    self.changed()


# ---------------------------------------------------------------------------

class ControlsPage(Page):
    def __init__(self, svc, on_change):
        super().__init__(svc, on_change)
        self.layout_.addWidget(heading("Controls"))
        bar = QHBoxLayout()
        self.unit = QComboBox()
        self.unit.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.unit)
        bar.addStretch()
        for text, slot in (("New control", self.add), ("Edit", self.edit), ("Delete", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            self.edit_buttons.append(b)
            bar.addWidget(b)
        self.layout_.addLayout(bar)
        self.table = Table(["Control", "Organisation", "Type", "Implementation", "Effectiveness", "Latest test",
                            "Risks"], numeric=[6])
        self.table.doubleClicked.connect(lambda *_: self.edit())
        self.layout_.addWidget(self.table)

    def refresh(self) -> None:
        unit_filter_combo(self.svc, self.unit)
        names = unit_names(self.svc)
        tests = self.svc.latest_control_tests()
        controls = self.svc.list_controls(self.unit.currentData())
        colors = {}
        rows = []
        for i, c in enumerate(controls):
            t = tests.get(c.id)
            rows.append([c.name, names.get(c.business_unit_id, "?"), title(c.control_type),
                         title(c.implementation_status),
                         "●" * (c.effectiveness_rating or 0) + "○" * (5 - (c.effectiveness_rating or 0))
                         if c.effectiveness_rating else "not rated",
                         f"{title(t.status)} · {t.date}" if t else "not tested", len(c.risk_ids)])
            if t:
                colors[(i, 5)] = {"PASSED": "#bbf7d0", "PARTIAL": "#fef08a", "FAILED": "#fecaca"}.get(t.status, "")
        self.table.fill(rows, keys=[c.id for c in controls], colors={k: v for k, v in colors.items() if v})

    def add(self):
        dialog = ControlDialog(self.svc, unit_id=self.unit.currentData(), parent=self)
        if dialog.exec():
            v = dialog.values()
            if guarded(self, lambda: self.svc.create_control(v.pop("business_unit_id"), v.pop("name"), **v)):
                self.changed()

    def edit(self):
        cid = self.table.selected_key()
        if not cid:
            return
        dialog = ControlDialog(self.svc, self.svc.get_control(cid), parent=self)
        if dialog.exec():
            with self.svc.context(reason=dialog.change_reason()):
                if guarded(self, lambda: self.svc.update_control(cid, **dialog.values())):
                    self.changed()

    def delete(self):
        cid = self.table.selected_key()
        if not cid:
            return
        control = self.svc.get_control(cid)
        reason, ok = QInputDialog.getText(self, "Delete control", f"Delete '{control.name}' and its test events?"
                                          "\n\nReason (recorded in the audit log):")
        if ok:
            with self.svc.context(reason=reason or None):
                if guarded(self, lambda: self.svc.delete_control(cid)) is not None:
                    self.changed()


# ---------------------------------------------------------------------------

class AnalysisPage(Page):
    STATS = [("mean", "Expected annual loss"), ("p50", "Median (P50)"), ("p90", "Bad year (P90)"),
             ("p99", "Value at Risk (P99)"), ("tvar99", "Expected shortfall (TVaR 99)")]

    def __init__(self, svc, on_change, defaults: Dict[str, Any]):
        super().__init__(svc, on_change)
        self.layout_.addWidget(heading("Analysis"))
        bar = QHBoxLayout()
        self.unit = QComboBox()
        self.engine = QComboBox()
        self.engine.addItem("Analytic (exact, FFT)", "analytic")
        self.engine.addItem("Monte Carlo simulation", "monte_carlo")
        self.engine.setCurrentIndex(self.engine.findData(defaults.get("engine", "analytic")))
        self.trials = QSpinBox()
        self.trials.setRange(1_000, 1_000_000)
        self.trials.setSingleStep(10_000)
        self.trials.setValue(defaults.get("n_trials", 50_000))
        self.trials.setSuffix(" years")
        self.seed = defaults.get("seed", 42)
        run = QPushButton("Run")
        run.clicked.connect(lambda: self.run(use_cache=True))
        recalc = QPushButton("Recalculate")
        recalc.clicked.connect(lambda: self.run(use_cache=False))
        name = QPushButton("Name this run…")
        name.clicked.connect(self.name_run)
        self.edit_buttons.append(name)
        for w in (QLabel("Scope"), self.unit, QLabel("Method"), self.engine, self.trials, run, recalc, name):
            bar.addWidget(w)
        bar.addStretch()
        self.engine.currentIndexChanged.connect(lambda: self.trials.setVisible(self.engine.currentData() == "monte_carlo"))
        self.trials.setVisible(self.engine.currentData() == "monte_carlo")
        self.layout_.addLayout(bar)
        self.run_info = QLabel("Choose scope and method, then press Run.")
        self.run_info.setWordWrap(True)
        self.layout_.addWidget(self.run_info)

        split = QSplitter(Qt.Vertical)
        top = QWidget()
        top_l = QHBoxLayout(top)
        self.stats = Table(["Figure", "Without controls", "With controls", "Reduction"], numeric=[1, 2, 3])
        top_l.addWidget(self.stats, 2)
        self.chart = QChartView()
        self.chart.setRenderHint(QPainter.Antialiasing)
        self.chart.setMinimumHeight(260)
        top_l.addWidget(self.chart, 3)
        split.addWidget(top)
        self.per_risk = Table(["Risk", "Expected", "P90", "P99", "P(loss)"], numeric=[1, 2, 3, 4])
        split.addWidget(self.per_risk)
        hist = QWidget()
        hist_l = QVBoxLayout(hist)
        hist_bar = QHBoxLayout()
        hist_bar.addWidget(QLabel("<b>Run history</b> – stored results; while the data is unchanged they are reused"))
        hist_bar.addStretch()
        show = QPushButton("Show selected")
        show.clicked.connect(self.show_selected)
        check = QPushButton("Recalculate && compare")
        check.clicked.connect(self.reproduce_selected)
        hist_bar.addWidget(show)
        hist_bar.addWidget(check)
        hist_l.addLayout(hist_bar)
        self.history = Table(["#", "When", "By", "Method", "Scope", "Name", "Status"], numeric=[0])
        self.history.doubleClicked.connect(lambda *_: self.show_selected())
        hist_l.addWidget(self.history)
        split.addWidget(hist)
        self.layout_.addWidget(split, 1)
        self.current_run: Optional[Dict[str, Any]] = None

    def refresh(self) -> None:
        unit_filter_combo(self.svc, self.unit)
        names = unit_names(self.svc)
        runs = [r for r in self.svc.list_runs() if r["mode"] == "compare"]
        self.history.fill(
            [[r["number"], r["created_at"][:16].replace("T", " "), r["created_by"],
              "Analytic" if r["engine"] == "analytic" else f"Monte Carlo ({r['params'].get('n_trials', 0):,})",
              names.get(r["params"].get("unit_id"), "All") if r["params"].get("unit_id") else "All",
              r.get("label") or "", "data changed" if r["stale"] else "current"] for r in runs],
            keys=[r["id"] for r in runs])
        if self.current_run and not self.current_run.get("_historic"):
            self.run(use_cache=True)  # data may have changed: show the matching run

    def run(self, use_cache: bool) -> None:
        engine = self.engine.currentData()
        result = guarded(self, lambda: self.svc.run_analysis(
            "compare", engine, self.trials.value(), self.seed, unit_id=self.unit.currentData(), use_cache=use_cache))
        if result:
            self.show_report(result)
            self.refresh_history_only()

    def refresh_history_only(self) -> None:
        current = self.current_run
        self.current_run = None  # avoid re-running from refresh()
        self.refresh()
        self.current_run = current

    def show_report(self, report: Dict[str, Any], historic: bool = False) -> None:
        run = report["run"]
        self.current_run = {**run, "_historic": historic}
        status = "data has changed since" if run["stale"] else "matches current data"
        how = "stored result" if (historic or run.get("cached")) else "calculated now"
        label = f" – <b>{run['label']}</b>" if run.get("label") else ""
        self.run_info.setText(
            f"<b>Run #{run['number']}</b>{label} · {how}, {run['created_at'][:16].replace('T', ' ')} by "
            f"{run['created_by']} · {status} · fingerprint <code>{run['fingerprint'][:12]}</code>")
        inh, res = report["inherent"]["total"], report["residual"]["total"]
        self.stats.fill([[label_, money(inh[k]), money(res[k]), money(inh[k] - res[k])] for k, label_ in self.STATS])
        self.per_risk.fill([[r["name"], money(r["mean"]), money(r["p90"]), money(r["p99"]),
                             f"{r['prob_any_loss'] * 100:.0f}%"] for r in report["residual"]["risks"]])
        self._draw_chart(report)

    def _draw_chart(self, report: Dict[str, Any]) -> None:
        chart = QChart()
        chart.setTitle("Loss exceedance: P(annual loss > amount)")
        x_axis = QLogValueAxis()
        x_axis.setLabelFormat("%.0g")
        x_axis.setTitleText("Annual loss")
        y_axis = QValueAxis()
        y_axis.setRange(0, 100)
        y_axis.setLabelFormat("%d%%")
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)
        for key, name, color in (("inherent", "Without controls", "#f97316"), ("residual", "With controls", "#3b82f6")):
            series = QLineSeries()
            series.setName(name)
            series.setPen(QPen(QColor(color), 2))
            for p in report[key]["loss_exceedance_curve"]:
                if p["loss"] > 0:
                    series.append(p["loss"], p["prob_exceed"] * 100)
            chart.addSeries(series)
            series.attachAxis(x_axis)
            series.attachAxis(y_axis)
        self.chart.setChart(chart)

    def name_run(self) -> None:
        if not self.current_run:
            return
        text, ok = QInputDialog.getText(self, "Name this run", "Name (e.g. 'Board meeting 1 Dec 2026'):",
                                        text=self.current_run.get("label") or "")
        if ok and guarded(self, lambda: self.svc.label_run(self.current_run["id"], text)):
            self.current_run["label"] = text or None
            self.show_report(self.svc.get_run(self.current_run["id"]), self.current_run.get("_historic", False))
            self.refresh_history_only()

    def show_selected(self) -> None:
        rid = self.history.selected_key()
        if rid:
            self.show_report(self.svc.get_run(rid), historic=True)

    def reproduce_selected(self) -> None:
        rid = self.history.selected_key()
        if rid:
            result = guarded(self, lambda: self.svc.reproduce_run(rid))
            if result:
                QMessageBox.information(self, "Recalculate", result["message"])


# ---------------------------------------------------------------------------

class AuditPage(Page):
    def __init__(self, svc, on_change):
        super().__init__(svc, on_change)
        self.layout_.addWidget(heading("Audit log"))
        bar = QHBoxLayout()
        verify = QPushButton("Verify integrity")
        verify.clicked.connect(self.verify)
        self.status = QLabel("")
        bar.addWidget(verify)
        bar.addWidget(self.status, 1)
        self.layout_.addLayout(bar)
        split = QSplitter(Qt.Vertical)
        self.table = Table(["#", "Time", "User", "Action", "Entity", "Details", "Reason", "Source"], numeric=[0])
        self.table.itemSelectionChanged.connect(self.show_details)
        split.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        split.addWidget(self.details)
        split.setSizes([500, 160])
        self.layout_.addWidget(split, 1)
        self.entries: Dict[str, Any] = {}

    def refresh(self) -> None:
        entries = self.svc.list_audit(limit=2000)
        self.entries = {e.id: e for e in entries}
        self.table.fill([[e.seq, e.timestamp[:19].replace("T", " "), e.user_id, title(e.action), title(e.entity_type),
                          e.details or "", e.reason or "", e.source] for e in entries], keys=[e.id for e in entries])

    def show_details(self) -> None:
        e = self.entries.get(self.table.selected_key())
        if not e:
            return
        lines = [f"{f}: {json.dumps(b, ensure_ascii=False)}  →  {json.dumps(a, ensure_ascii=False)}"
                 for f, (b, a) in (e.changes or {}).items()]
        lines += ["", f"source: {e.source}", f"entity: {e.entity_id}", f"state:  {e.state_hash}",
                  f"hash:   {e.hash}", f"prev:   {e.prev_hash}"]
        self.details.setPlainText("\n".join(lines))

    def verify(self) -> None:
        result = guarded(self, self.svc.verify_audit)
        if result is None:
            return
        if result["ok"]:
            self.status.setText(f"<span style='color:#15803d'><b>Audit log intact</b></span> – {result['entries']} "
                                f"entries, head #{result['head_seq']} <code>{result['head_hash'][:16]}…</code>")
        else:
            problems = [f"#{e['seq']}: {e['error']}" for e in result["chain_errors"]]
            problems += [f"{e['entity_type']} {e['entity_id'][:8]}: {e['error']}" for e in result["data_errors"]]
            self.status.setText(f"<span style='color:#b91c1c'><b>{len(problems)} integrity problems</b></span>")
            QMessageBox.warning(self, "Audit log", "\n".join(problems[:30]))



# ---------------------------------------------------------------------------

class ReportsPage(Page):
    """Closed periods, development over time and the comparison of two periods."""

    def __init__(self, svc, on_change):
        super().__init__(svc, on_change)
        self.admin_buttons: list = []
        self.layout_.addWidget(heading("Reports"))
        self.layout_.addWidget(QLabel("Closing a period freezes its figures; reports always show what was reported. "
                                      "Periods: 2026-11 (month), 2026-Q4 (quarter), 2026 (year)."))
        bar = QHBoxLayout()
        self.key = QLineEdit()
        self.key.setPlaceholderText("2026-Q4")
        self.key.setMaximumWidth(120)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Note (optional)")
        close = QPushButton("Close period")
        close.clicked.connect(self.close_period)
        reopen = QPushButton("Reopen selected")
        reopen.clicked.connect(self.reopen_period)
        self.admin_buttons += [close, reopen]
        for w in (QLabel("Period"), self.key, self.note, close, reopen):
            bar.addWidget(w)
        bar.addStretch()
        self.layout_.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        top = QWidget()
        top_l = QHBoxLayout(top)
        self.periods = Table(["Period", "Status", "Closed", "By", "Expected loss", "Anchored", "Note"], numeric=[4])
        top_l.addWidget(self.periods, 3)
        self.trend = Table(["Period", "Expected loss", "P99", "Risks", "Actual losses"], numeric=[1, 2, 3, 4])
        top_l.addWidget(self.trend, 2)
        split.addWidget(top)

        cmp_w = QWidget()
        cmp_l = QVBoxLayout(cmp_w)
        cmp_bar = QHBoxLayout()
        self.a, self.b = QComboBox(), QComboBox()
        go = QPushButton("Compare")
        go.clicked.connect(self.compare)
        for w in (QLabel("<b>Compare</b>"), self.a, QLabel("→"), self.b, go):
            cmp_bar.addWidget(w)
        cmp_bar.addStretch()
        cmp_l.addLayout(cmp_bar)
        row = QHBoxLayout()
        self.bridge = Table(["Bridge (expected loss after controls)", "Amount"], numeric=[1])
        self.changes = Table(["Risk", "Status", "Before", "After", "Change", "Heat map"], numeric=[2, 3, 4])
        row.addWidget(self.bridge, 2)
        row.addWidget(self.changes, 5)
        cmp_l.addLayout(row)
        split.addWidget(cmp_w)
        self.layout_.addWidget(split, 1)

    def refresh(self) -> None:
        periods = self.svc.list_periods()
        totals = {f.period_key: f for f in self.svc.period_facts(level="total")}
        self.periods.fill([[p.key, p.status, (p.closed_at or "")[:10], p.closed_by or "",
                            money(totals[p.key].eal_residual) if p.key in totals else "–",
                            "yes" if p.anchor_id else "", p.note or ""] for p in periods],
                          keys=[p.key for p in periods])
        closed = sorted((p for p in periods if p.status == "closed"), key=lambda p: p.end)
        self.trend.fill([[p.key, money(totals[p.key].eal_residual), money(totals[p.key].p99),
                          totals[p.key].risk_count, money(totals[p.key].actual_loss)] for p in closed])
        for combo, default in ((self.a, -2), (self.b, -1)):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([p.key for p in closed])
            if current and combo.findText(current) >= 0:
                combo.setCurrentText(current)
            elif len(closed) >= 2:
                combo.setCurrentIndex(len(closed) + default)
            combo.blockSignals(False)

    def compare(self) -> None:
        if not self.a.currentText() or not self.b.currentText():
            return
        c = guarded(self, lambda: self.svc.compare_periods(self.a.currentText(), self.b.currentText()))
        if not c:
            return
        b = c["bridge"]
        self.bridge.fill([[f"Expected loss {c['a']}", money(b["start"])], ["+ new risks", money(b["new_risks"])],
                          ["− removed risks", money(b["removed_risks"])],
                          ["± changed estimates", money(b["changed_estimates"])],
                          ["± controls", money(b["controls"])], [f"Expected loss {c['b']}", money(b["end"])]])

        def hm(v):
            return f"{v[0]}×{v[1]}" if v else "–"
        rows = [[r["name"], r["status"], money(r["eal_residual"]["a"]), money(r["eal_residual"]["b"]),
                 money(r["eal_residual"]["change"]), f"{hm(r['heatmap']['a'])} → {hm(r['heatmap']['b'])}"]
                for r in c["risks"]]
        colors = {(i, 1): {"new": "#fed7aa", "removed": "#bbf7d0", "changed": "#fef08a"}.get(r["status"], "")
                  for i, r in enumerate(c["risks"])}
        self.changes.fill(rows, colors={k: v for k, v in colors.items() if v})

    def close_period(self) -> None:
        key = self.key.text().strip()
        if not key:
            return
        period = guarded(self, lambda: self.svc.close_period(key, self.note.text().strip() or None))
        if period:
            QMessageBox.information(self, "Period closed", f"{period.key} closed; figures frozen"
                                    + (" and audit chain anchored." if period.anchor_id else "."))
            self.key.clear()
            self.note.clear()
            self.changed()

    def reopen_period(self) -> None:
        key = self.periods.selected_key()
        if key and QMessageBox.question(self, "Reopen", f"Reopen {key}? Its frozen figures are removed "
                                        "(recorded in the audit log).") == QMessageBox.Yes:
            if guarded(self, lambda: self.svc.reopen_period(key)):
                self.changed()


def _editor_only(method):
    """Viewers can open pages but not change anything (double-clicks included)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.can_edit:
            QMessageBox.information(self, "RiskMesh", "Your role is 'viewer': you can look, but not change data.")
            return None
        return method(self, *args, **kwargs)
    return wrapper


for _cls, _names in ((OrganisationPage, ("add", "add_child", "edit", "delete")),
                     (RisksPage, ("add", "edit", "delete")), (ControlsPage, ("add", "edit", "delete")),
                     (AnalysisPage, ("name_run",))):
    for _name in _names:
        setattr(_cls, _name, _editor_only(getattr(_cls, _name)))
