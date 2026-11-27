"""Shared helpers for the desktop GUI: formatting, tables, error handling, distribution editors."""

import re
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QHeaderView, QLabel, QMessageBox, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..domain.errors import DomainError

LEVEL_COLORS = {"Very High": "#fecaca", "High": "#fed7aa", "Medium": "#fef08a", "Low": "#bbf7d0", "Very Low": "#bbf7d0"}


def money(value: Optional[float]) -> str:
    """1234567.8 -> '1.234.568' (Danish thousands separator)."""
    if value is None:
        return "–"
    return f"{value:,.0f}".replace(",", ".")


def compact(value: Optional[float]) -> str:
    if value is None:
        return "–"
    a = abs(value)
    if a >= 1e9:
        return f"{value / 1e9:.1f} mia.".replace(".", ",", 1)
    if a >= 1e6:
        return f"{value / 1e6:.1f} mio.".replace(".", ",", 1)
    if a >= 1e3:
        return f"{value / 1e3:.0f} t."
    return money(value)


def title(text: Optional[str]) -> str:
    return (text or "").replace("_", " ").capitalize()


def score_level(score: float) -> str:
    return ("Very High" if score >= 16 else "High" if score >= 9 else "Medium" if score >= 4
            else "Low" if score >= 2 else "Very Low")


def describe_frequency(f: Optional[Dict[str, Any]]) -> str:
    if not f:
        return "–"
    return f"{f['rate']:g} / year" if f["type"] == "poisson" else f"{f['p'] * 100:g}% chance"


def describe_impact(i: Optional[Dict[str, Any]]) -> str:
    if not i:
        return "–"
    d = i.get("dist")
    if d == "lognorm_p10_p90":
        return f"{compact(i['p10'])} – {compact(i['p90'])}"
    if d in ("pert", "triangular"):
        return f"{compact(i['min'])} / {compact(i['mode'])} / {compact(i['max'])}"
    if d == "uniform":
        return f"{compact(i['min'])} – {compact(i['max'])}"
    if d == "fixed":
        return compact(i["value"])
    return str(d)


def show_error(parent: Optional[QWidget], error: Exception) -> None:
    QMessageBox.warning(parent, "RiskMesh", str(error))


@contextmanager
def busy():
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


def guarded(parent: QWidget, fn: Callable[[], Any]) -> Any:
    """Runs fn and shows domain/validation errors as a message instead of crashing."""
    try:
        with busy():
            return fn()
    except (DomainError, ValueError, OSError) as e:
        show_error(parent, e)
        return None


def _number(text: str) -> float:
    """'1.234.568' / '63%' / '#4' -> number for sorting (Danish thousands separator)."""
    cleaned = re.sub(r"[^\d,\-]", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return float("-inf")


class _NumericItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        return _number(self.text()) < _number(other.text())


class Table(QTableWidget):
    """
    Read-only table filled from rows of values; row data kept in Qt.UserRole of column 0.
    Rows keep the order they are given in until the user clicks a column header;
    numeric columns then sort by value, not text.
    """

    def __init__(self, headers: Sequence[str], parent: Optional[QWidget] = None, numeric: Iterable[int] = ()):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(list(headers))
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setStretchLastSection(False)
        self.numeric = set(numeric)

    def fill(self, rows: List[Sequence[Any]], keys: Optional[List[Any]] = None,
             colors: Optional[Dict[tuple, str]] = None) -> None:
        self.setSortingEnabled(False)
        self.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)  # keep the given order
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                text = "" if value is None else str(value)
                item = _NumericItem(text) if c in self.numeric else QTableWidgetItem(text)
                if c in self.numeric:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if colors and (r, c) in colors:
                    item.setBackground(QColor(colors[(r, c)]))
                if c == 0 and keys is not None:
                    item.setData(Qt.UserRole, keys[r])
                self.setItem(r, c, item)
        self.resizeColumnsToContents()
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)  # names get the spare width
        self.setSortingEnabled(True)

    def selected_key(self) -> Any:
        row = self.currentRow()
        if row < 0:
            return None
        item = self.item(row, 0)
        return item.data(Qt.UserRole) if item else None


class Card(QFrame):
    """A key-figure tile: small caption, big value, hint."""

    def __init__(self, caption: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("Card { background: white; border: 1px solid #e5e7eb; border-radius: 8px; }")
        layout = QVBoxLayout(self)
        self.caption = QLabel(caption)
        self.caption.setStyleSheet("color: #6b7280;")
        self.value = QLabel("–")
        f = QFont()
        f.setPointSize(18)
        f.setBold(True)
        self.value.setFont(f)
        self.hint = QLabel("")
        self.hint.setStyleSheet("color: #6b7280; font-size: 11px;")
        self.hint.setWordWrap(True)
        for w in (self.caption, self.value, self.hint):
            layout.addWidget(w)

    def set(self, value: str, hint: str = "") -> None:
        self.value.setText(value)
        self.hint.setText(hint)


def heading(text: str) -> QLabel:
    label = QLabel(text)
    f = QFont()
    f.setPointSize(15)
    f.setBold(True)
    label.setFont(f)
    return label


def money_spin(value: float = 0.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(0, 1e12)
    spin.setDecimals(0)
    spin.setSingleStep(10_000)
    spin.setGroupSeparatorShown(True)
    spin.setValue(value)
    return spin


HOURS_DEFAULTS = {"p10": 1, "p90": 24, "min": 1, "mode": 4, "max": 48, "value": 8}


def hours_spin(value: float = 0.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(0, 24 * 365)
    spin.setDecimals(1)
    spin.setSingleStep(1)
    spin.setSuffix(" h")
    spin.setValue(value)
    return spin


class FrequencyEditor(QWidget):
    """Events per year (Poisson) or chance per year (Bernoulli)."""
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        self.kind.addItem("Events per year (can repeat)", "poisson")
        self.kind.addItem("Chance per year (at most once)", "bernoulli")
        self.value = QDoubleSpinBox()
        self.value.setDecimals(3)
        self.value.setRange(0, 1000)
        self.value_label = QLabel()
        form.addRow("Frequency model", self.kind)
        form.addRow(self.value_label, self.value)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.value.valueChanged.connect(self.changed)
        self._kind_changed()

    def _kind_changed(self) -> None:
        if self.kind.currentData() == "poisson":
            self.value_label.setText("Average events / year")
            self.value.setRange(0, 1000)
            self.value.setSuffix("")
        else:
            self.value_label.setText("Probability / year")
            self.value.setRange(0, 100)
            self.value.setSuffix(" %")
        self.changed.emit()

    def set_spec(self, spec: Dict[str, Any]) -> None:
        self.kind.setCurrentIndex(0 if spec["type"] == "poisson" else 1)
        self.value.setValue(spec["rate"] if spec["type"] == "poisson" else spec["p"] * 100)

    def spec(self) -> Dict[str, Any]:
        if self.kind.currentData() == "poisson":
            return {"type": "poisson", "rate": self.value.value()}
        return {"type": "bernoulli", "p": self.value.value() / 100}


IMPACT_FIELDS = {
    "lognorm_p10_p90": [("p10", "Low estimate (P10)", 50_000), ("p90", "High estimate (P90)", 1_000_000)],
    "pert": [("min", "Minimum", 10_000), ("mode", "Most likely", 100_000), ("max", "Maximum", 1_000_000)],
    "triangular": [("min", "Minimum", 10_000), ("mode", "Most likely", 100_000), ("max", "Maximum", 1_000_000)],
    "uniform": [("min", "Minimum", 10_000), ("max", "Maximum", 100_000)],
    "fixed": [("value", "Loss", 100_000)],
}
IMPACT_LABELS = {
    "lognorm_p10_p90": "Range: low / high estimate (lognormal)",
    "pert": "Three-point estimate (PERT)",
    "triangular": "Three-point estimate (triangular)",
    "uniform": "Anywhere between min and max",
    "fixed": "Fixed amount",
}


class ImpactEditor(QWidget):
    """Loss per event (or, with hours=True, outage duration): one page of fields per distribution type."""
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, caption: str = "Loss per event", hours: bool = False):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QFormLayout()
        self.kind = QComboBox()
        for key, label in IMPACT_LABELS.items():
            self.kind.addItem(label.replace("amount", "duration") if hours else label, key)
        top.addRow(caption, self.kind)
        layout.addLayout(top)
        self.pages = QStackedWidget()
        self.fields: Dict[str, Dict[str, QDoubleSpinBox]] = {}
        for key, specs in IMPACT_FIELDS.items():
            page = QWidget()
            form = QFormLayout(page)
            form.setContentsMargins(0, 0, 0, 0)
            self.fields[key] = {}
            for name, label, default in specs:
                spin = hours_spin(HOURS_DEFAULTS.get(name, 4)) if hours else money_spin(default)
                spin.valueChanged.connect(self.changed)
                form.addRow(label, spin)
                self.fields[key][name] = spin
            self.pages.addWidget(page)
        layout.addWidget(self.pages)
        self.custom: Optional[Dict[str, Any]] = None
        self.kind.currentIndexChanged.connect(lambda i: (self.pages.setCurrentIndex(i), self.changed.emit()))

    def set_spec(self, spec: Dict[str, Any]) -> None:
        key = spec.get("dist")
        if key not in IMPACT_FIELDS:  # e.g. a scipy distribution entered via the API: keep it untouched
            self.custom = dict(spec)
            self.kind.setEnabled(False)
            self.pages.setEnabled(False)
            self.kind.setToolTip(f"Custom distribution '{key}' - edit it via the API or CLI")
            return
        self.kind.setCurrentIndex(list(IMPACT_FIELDS).index(key))
        for name, spin in self.fields[key].items():
            spin.setValue(float(spec[name]))

    def spec(self) -> Dict[str, Any]:
        if self.custom is not None:
            return dict(self.custom)
        key = self.kind.currentData()
        return {"dist": key, **{name: spin.value() for name, spin in self.fields[key].items()}}
