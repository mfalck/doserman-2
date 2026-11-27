"""Edit dialogs. They only collect values; the service validates and saves."""

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QSpinBox, QVBoxLayout,
                               QWidget)

from ..domain import risk_model
from ..domain.models import (CONTROL_TYPES, IMPLEMENTATION_STATUSES, ORGANIZATION_TYPES, REGULATIONS,
                             SERVICE_CRITICALITIES, SUPPLIER_TIERS, BusinessUnit, Control, Customer, Risk, Service,
                             Supplier)
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



def _check_list(items, linked, label=lambda x: x.name, height=110) -> QListWidget:
    lst = QListWidget()
    lst.setFixedHeight(height)
    for x in items:
        item = QListWidgetItem(label(x))
        item.setData(Qt.UserRole, getattr(x, "id", x))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if getattr(x, "id", x) in linked else Qt.Unchecked)
        lst.addItem(item)
    return lst


def _checked(lst: QListWidget) -> list:
    return [lst.item(i).data(Qt.UserRole) for i in range(lst.count()) if lst.item(i).checkState() == Qt.Checked]


def _enum_combo(values, selected) -> QComboBox:
    combo = QComboBox()
    for v in values:
        combo.addItem(title(v), v)
    combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


def _csv(values) -> str:
    return ", ".join(values or [])


def _split(text: str) -> list:
    return [x.strip() for x in text.split(",") if x.strip()]


def _hours(value) -> QLineEdit:
    w = QLineEdit("" if value is None else f"{value:g}")
    w.setPlaceholderText("hours")
    return w


def _float(w: QLineEdit):
    text = w.text().strip().replace(",", ".").replace(" ", "")
    return float(text) if text else None


class SupplierDialog(BaseDialog):
    def __init__(self, svc: RiskService, supplier: Optional[Supplier] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit supplier" if supplier else "New supplier", supplier is not None, parent)
        s = supplier
        self.name = QLineEdit(s.name if s else "")
        self.tier = _enum_combo(SUPPLIER_TIERS, s.tier if s else "STANDARD")
        self.country = QLineEdit((s.country or "") if s else "")
        self.owner = _person_combo(svc, s.owner_id if s else None)
        self.contract_start = QLineEdit((s.contract_start or "") if s else "")
        self.contract_end = QLineEdit((s.contract_end or "") if s else "")
        self.next_review = QLineEdit((s.next_review or "") if s else "")
        for w in (self.contract_start, self.contract_end, self.next_review):
            w.setPlaceholderText("YYYY-MM-DD")
        self.personal = QCheckBox("Processes personal data")
        self.personal.setChecked(bool(s and s.processes_personal_data))
        self.exit_plan = QCheckBox("Exit plan in place")
        self.exit_plan.setChecked(bool(s and s.exit_plan))
        self.locations = QLineEdit(_csv(s.data_locations) if s else "")
        self.locations.setPlaceholderText("e.g. DK, EU")
        self.certs = QLineEdit(_csv(s.certifications) if s else "")
        self.certs.setPlaceholderText("e.g. ISO 27001, ISAE 3402")
        self.subs = _check_list([x for x in svc.list_suppliers() if not s or x.id != s.id],
                                set(s.subcontractor_ids) if s else set())
        self.description = QPlainTextEdit((s.description or "") if s else "")
        self.description.setFixedHeight(50)
        for label, w in (("Name", self.name), ("Tier", self.tier), ("Country", self.country),
                         ("Relationship owner", self.owner), ("Contract start", self.contract_start),
                         ("Contract end", self.contract_end), ("Next review", self.next_review),
                         ("", self.personal), ("Data locations", self.locations),
                         ("Certifications / reports", self.certs), ("", self.exit_plan),
                         ("Its subcontractors", self.subs), ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "tier": self.tier.currentData(),
                "country": self.country.text().strip() or None, "owner_id": self.owner.currentData(),
                "contract_start": self.contract_start.text().strip() or None,
                "contract_end": self.contract_end.text().strip() or None,
                "next_review": self.next_review.text().strip() or None,
                "processes_personal_data": self.personal.isChecked(), "exit_plan": self.exit_plan.isChecked(),
                "data_locations": _split(self.locations.text()), "certifications": _split(self.certs.text()),
                "subcontractor_ids": _checked(self.subs),
                "description": self.description.toPlainText().strip() or None}


class ServiceDialog(BaseDialog):
    def __init__(self, svc: RiskService, service: Optional[Service] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit service" if service else "New service", service is not None, parent)
        s = service
        self.name = QLineEdit(s.name if s else "")
        self.unit = _unit_combo(svc, s.business_unit_id if s else None)
        self.criticality = _enum_combo(SERVICE_CRITICALITIES, s.criticality if s else "MEDIUM")
        self.owner = _person_combo(svc, s.owner_id if s else None)
        self.rto = _hours(s.rto_hours if s else None)
        self.rpo = _hours(s.rpo_hours if s else None)
        self.suppliers = _check_list(svc.list_suppliers(), set(s.supplier_ids) if s else set())
        self.assets = _check_list(svc.list_assets(), set(s.asset_ids) if s else set(), height=90)
        self.description = QPlainTextEdit((s.description or "") if s else "")
        self.description.setFixedHeight(50)
        for label, w in (("Name", self.name), ("Delivered by", self.unit), ("Criticality", self.criticality),
                         ("Owner", self.owner), ("Recovery time (RTO)", self.rto),
                         ("Recovery point (RPO)", self.rpo), ("Depends on suppliers", self.suppliers),
                         ("Runs on assets", self.assets), ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "business_unit_id": self.unit.currentData(),
                "criticality": self.criticality.currentData(), "owner_id": self.owner.currentData(),
                "rto_hours": _float(self.rto), "rpo_hours": _float(self.rpo),
                "supplier_ids": _checked(self.suppliers), "asset_ids": _checked(self.assets),
                "description": self.description.toPlainText().strip() or None}


class CustomerDialog(BaseDialog):
    def __init__(self, svc: RiskService, customer: Optional[Customer] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit customer" if customer else "New customer", customer is not None, parent)
        c = customer
        self.name = QLineEdit(c.name if c else "")
        self.sector = QComboBox()
        self.sector.setEditable(True)
        self.sector.addItems(["", "municipal", "public", "energy", "finance", "health", "transport", "private"])
        self.sector.setCurrentText((c.sector or "") if c else "")
        self.regulations = _check_list(REGULATIONS, set(c.regulations) if c else set(),
                                       label=lambda r: r.replace("_", " "), height=90)
        self.critical = QCheckBox("Our services support a critical or important function (DORA)")
        self.critical.setChecked(bool(c and c.critical_function))
        self.services = _check_list(svc.list_services(), set(c.service_ids) if c else set(), height=90)
        self.owner = _person_combo(svc, c.account_owner_id if c else None)
        self.value = QLineEdit("" if not c or c.contract_value is None else f"{c.contract_value:g}")
        self.value.setPlaceholderText("per year")
        self.contract_end = QLineEdit((c.contract_end or "") if c else "")
        self.contract_end.setPlaceholderText("YYYY-MM-DD")
        self.notify = _hours(c.notification_hours if c else None)
        self.audit = QCheckBox("Audit rights")
        self.audit.setChecked(bool(c and c.audit_rights))
        self.exit = QCheckBox("Exit support")
        self.exit.setChecked(bool(c and c.exit_support))
        self.notes = QPlainTextEdit((c.notes or "") if c else "")
        self.notes.setFixedHeight(50)
        for label, w in (("Name", self.name), ("Sector", self.sector), ("Regulation", self.regulations),
                         ("", self.critical), ("Uses services", self.services), ("Account owner", self.owner),
                         ("Contract value", self.value), ("Contract end", self.contract_end),
                         ("Notify incidents within", self.notify), ("Contract grants", self.audit),
                         ("", self.exit), ("Notes", self.notes)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "sector": self.sector.currentText().strip() or None,
                "regulations": _checked(self.regulations), "critical_function": self.critical.isChecked(),
                "service_ids": _checked(self.services), "account_owner_id": self.owner.currentData(),
                "contract_value": _float(self.value), "contract_end": self.contract_end.text().strip() or None,
                "notification_hours": _float(self.notify), "audit_rights": self.audit.isChecked(),
                "exit_support": self.exit.isChecked(), "notes": self.notes.toPlainText().strip() or None}


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
