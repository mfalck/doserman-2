"""Edit dialogs. They only collect values; the service validates and saves."""

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QSpinBox, QVBoxLayout,
                               QWidget)

from ..domain import risk_model
from ..domain.models import CONTROL_TYPES, IMPLEMENTATION_STATUSES, ORGANIZATION_TYPES, BusinessUnit, Control, Risk
from ..services import RiskService
from .widgets import LEVEL_COLORS, FrequencyEditor, ImpactEditor, money, title

RISK_CATEGORIES = ["strategic", "operational", "financial", "compliance", "reputation", "cyber", "hazard"]


def _unit_combo(svc: RiskService, selected: Optional[str], allow_none: bool = False) -> QComboBox:
    combo = QComboBox()
    if allow_none:
        combo.addItem("(none - top level)", None)
    for u in svc.list_business_units():
        combo.addItem(f"{u.name} ({u.code})" if u.code else u.name, u.id)
    if selected:
        combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


def _person_combo(svc: RiskService, selected: Optional[str]) -> QComboBox:
    combo = QComboBox()
    combo.addItem("(no owner)", None)
    for p in svc.list_persons():
        combo.addItem(f"{p.name} – {p.title}" if p.title else p.name, p.id)
    if selected:
        combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


class BaseDialog(QDialog):
    """Form + optional 'reason for change' + OK/Cancel."""

    def __init__(self, title_text: str, editing: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title_text)
        self.setMinimumWidth(520)
        self.layout_ = QVBoxLayout(self)
        self.form = QFormLayout()
        self.layout_.addLayout(self.form)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Recorded in the audit log, e.g. annual review")
        self.editing = editing

    def finish(self) -> None:
        if self.editing:
            box = QFormLayout()
            box.addRow("Reason for change", self.reason)
            self.layout_.addLayout(box)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.layout_.addWidget(buttons)

    def change_reason(self) -> Optional[str]:
        return self.reason.text().strip() or None


class UnitDialog(BaseDialog):
    def __init__(self, svc: RiskService, unit: Optional[BusinessUnit] = None, parent_id: Optional[str] = None,
                 parent: Optional[QWidget] = None):
        super().__init__("Edit organisation unit" if unit else "New organisation unit", unit is not None, parent)
        self.name = QLineEdit(unit.name if unit else "")
        self.code = QLineEdit((unit.code or "") if unit else "")
        self.kind = QComboBox()
        for t in ORGANIZATION_TYPES:
            self.kind.addItem(title(t), t)
        self.kind.setCurrentIndex(self.kind.findData(unit.organization_type if unit else "DEPARTMENT"))
        self.parent_unit = _unit_combo(svc, unit.parent_id if unit else parent_id, allow_none=True)
        self.owner = _person_combo(svc, unit.risk_owner_id if unit else None)
        self.description = QPlainTextEdit((unit.description or "") if unit else "")
        self.description.setFixedHeight(60)
        for label, w in (("Name", self.name), ("Code", self.code), ("Type", self.kind),
                         ("Part of", self.parent_unit), ("Risk owner", self.owner), ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "code": self.code.text().strip() or None,
                "organization_type": self.kind.currentData(), "parent_id": self.parent_unit.currentData(),
                "risk_owner_id": self.owner.currentData(),
                "description": self.description.toPlainText().strip() or None}


class RiskDialog(BaseDialog):
    def __init__(self, svc: RiskService, risk: Optional[Risk] = None, unit_id: Optional[str] = None,
                 parent: Optional[QWidget] = None):
        super().__init__("Edit risk" if risk else "New risk", risk is not None, parent)
        self.svc = svc
        self.name = QLineEdit(risk.name if risk else "")
        self.unit = _unit_combo(svc, risk.business_unit_id if risk else unit_id)
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItem("")
        self.category.addItems(RISK_CATEGORIES)
        self.category.setCurrentText((risk.category or "") if risk else "")
        self.owner = _person_combo(svc, risk.owner_id if risk else None)
        self.description = QPlainTextEdit((risk.description or "") if risk else "")
        self.description.setFixedHeight(50)
        for label, w in (("Name", self.name), ("Organisation", self.unit), ("Risk type", self.category),
                         ("Risk owner", self.owner), ("Description", self.description)):
            self.form.addRow(label, w)

        inherent = QGroupBox("Inherent risk (without controls)")
        lay = QVBoxLayout(inherent)
        self.freq = FrequencyEditor()
        self.impact = ImpactEditor()
        lay.addWidget(self.freq)
        lay.addWidget(self.impact)
        self.layout_.addWidget(inherent)

        self.has_residual = QCheckBox("Controls change the estimate (residual risk)")
        self.layout_.addWidget(self.has_residual)
        self.residual_box = QGroupBox("Residual risk (after controls)")
        lay2 = QVBoxLayout(self.residual_box)
        self.res_freq = FrequencyEditor()
        self.res_impact = ImpactEditor()
        lay2.addWidget(self.res_freq)
        lay2.addWidget(self.res_impact)
        self.layout_.addWidget(self.residual_box)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        self.layout_.addWidget(self.preview)

        if risk:
            self.freq.set_spec(risk.frequency)
            self.impact.set_spec(risk.impact)
            residual = bool(risk.residual_frequency or risk.residual_impact)
            self.has_residual.setChecked(residual)
            self.res_freq.set_spec(risk.residual_frequency or risk.frequency)
            self.res_impact.set_spec(risk.residual_impact or risk.impact)
        else:
            self.freq.set_spec({"type": "poisson", "rate": 0.5})
            self.res_freq.set_spec({"type": "poisson", "rate": 0.5})
        self.residual_box.setVisible(self.has_residual.isChecked())
        self.has_residual.toggled.connect(lambda on: (self.residual_box.setVisible(on), self._update_preview(),
                                                      self.adjustSize()))
        for editor in (self.freq, self.impact, self.res_freq, self.res_impact):
            editor.changed.connect(self._update_preview)
        self._update_preview()
        self.finish()

    def _score_text(self, label: str, freq: Dict[str, Any], impact: Dict[str, Any]) -> str:
        try:
            s = risk_model.risk_scores(freq, impact, self.svc.scoring.likelihood_bands, self.svc.scoring.impact_bands)
        except (ValueError, TypeError) as e:
            return f"<b>{label}:</b> <span style='color:#b91c1c'>{e}</span>"
        color = LEVEL_COLORS.get(s["level"], "#eee")
        return (f"<b>{label}:</b> <span style='background:{color}'>&nbsp;{s['score']} ({s['likelihood']} × "
                f"{s['impact']}) {s['level']}&nbsp;</span> &nbsp; expected loss {money(s['expected_annual_loss'])} "
                f"/ year, {s['annual_probability'] * 100:.0f}% chance of at least one event")

    def _update_preview(self) -> None:
        lines = [self._score_text("Inherent", self.freq.spec(), self.impact.spec())]
        if self.has_residual.isChecked():
            lines.append(self._score_text("Residual", self.res_freq.spec(), self.res_impact.spec()))
        self.preview.setText("<br>".join(lines))

    def values(self) -> Dict[str, Any]:
        residual = self.has_residual.isChecked()
        return {
            "name": self.name.text(),
            "business_unit_id": self.unit.currentData(),
            "category": self.category.currentText().strip() or None,
            "owner_id": self.owner.currentData(),
            "description": self.description.toPlainText().strip() or None,
            "frequency": self.freq.spec(),
            "impact": self.impact.spec(),
            "residual_frequency": self.res_freq.spec() if residual else None,
            "residual_impact": self.res_impact.spec() if residual else None,
        }


class ControlDialog(BaseDialog):
    def __init__(self, svc: RiskService, control: Optional[Control] = None, unit_id: Optional[str] = None,
                 parent: Optional[QWidget] = None):
        super().__init__("Edit control" if control else "New control", control is not None, parent)
        self.name = QLineEdit(control.name if control else "")
        self.unit = _unit_combo(svc, control.business_unit_id if control else unit_id)
        self.kind = QComboBox()
        for t in CONTROL_TYPES:
            self.kind.addItem(title(t), t)
        self.kind.setCurrentIndex(self.kind.findData(control.control_type if control else "PREVENTIVE"))
        self.status = QComboBox()
        for t in IMPLEMENTATION_STATUSES:
            self.status.addItem(title(t), t)
        self.status.setCurrentIndex(self.status.findData(control.implementation_status if control else "PLANNED"))
        self.rating = QSpinBox()
        self.rating.setRange(0, 5)
        self.rating.setSpecialValueText("Not rated")
        self.rating.setValue((control.effectiveness_rating or 0) if control else 0)
        self.owner = _person_combo(svc, control.owner_id if control else None)
        self.description = QPlainTextEdit((control.description or "") if control else "")
        self.description.setFixedHeight(50)
        self.risks = QListWidget()
        self.risks.setFixedHeight(140)
        units = {u.id: u.name for u in svc.list_business_units()}
        linked = set(control.risk_ids) if control else set()
        for r in svc.list_risks():
            item = QListWidgetItem(f"{r.name}  ({units.get(r.business_unit_id, '?')})")
            item.setData(Qt.UserRole, r.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if r.id in linked else Qt.Unchecked)
            self.risks.addItem(item)
        for label, w in (("Name", self.name), ("Organisation", self.unit), ("Type", self.kind),
                         ("Implementation", self.status), ("Effectiveness (1-5)", self.rating),
                         ("Owner", self.owner), ("Description", self.description),
                         ("Mitigates these risks", self.risks)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        risk_ids = [self.risks.item(i).data(Qt.UserRole) for i in range(self.risks.count())
                    if self.risks.item(i).checkState() == Qt.Checked]
        return {"name": self.name.text(), "business_unit_id": self.unit.currentData(),
                "control_type": self.kind.currentData(), "implementation_status": self.status.currentData(),
                "effectiveness_rating": self.rating.value() or None, "owner_id": self.owner.currentData(),
                "description": self.description.toPlainText().strip() or None, "risk_ids": risk_ids}


class LoginDialog(QDialog):
    """Local RiskMesh account. (Single sign-on is for the web app; ask an admin for a local account.)"""

    def __init__(self, svc: RiskService, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.svc = svc
        self.user = None
        self.setWindowTitle("Sign in to RiskMesh")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        self.message = QLabel("")
        self.message.setStyleSheet("color: #b91c1c;")
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Sign in")
        buttons.accepted.connect(self.try_login)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def try_login(self) -> None:
        from ..domain.errors import AuthenticationError
        try:
            self.user = self.svc.authenticate(self.username.text().strip(), self.password.text())
        except AuthenticationError as e:
            self.message.setText(str(e))
            self.password.clear()
            return
        self.accept()
