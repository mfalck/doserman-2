"""Edit dialogs. They only collect values; the service validates and saves."""

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..domain import risk_model
from ..domain.models import (CONTROL_TYPES, IMPLEMENTATION_STATUSES, ORGANIZATION_TYPES, REGULATIONS,
                             SERVICE_CRITICALITIES, SUPPLIER_TIERS, INCIDENT_STATUSES, OBJECTIVE_CRITICALITIES, Appetite, Benchmark,
                             BusinessUnit, Control, Correlation, Customer, Incident, Objective, Observation, Risk,
                             Service, Supplier)
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
        self.lei = QLineEdit((unit.lei or "") if unit else "")
        self.lei.setPlaceholderText("Legal Entity Identifier, for a company (customers' DORA registers)")
        self.country = QLineEdit((unit.country or "") if unit else "")
        self.country.setPlaceholderText("e.g. DK")
        for label, w in (("Name", self.name), ("Code", self.code), ("Type", self.kind),
                         ("Part of", self.parent_unit), ("Risk owner", self.owner), ("LEI", self.lei),
                         ("Country", self.country), ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "code": self.code.text().strip() or None,
                "organization_type": self.kind.currentData(), "parent_id": self.parent_unit.currentData(),
                "risk_owner_id": self.owner.currentData(), "lei": self.lei.text().strip() or None,
                "country": self.country.text().strip() or None,
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
        self.evidence = QLineEdit(_csv(risk.evidence_categories) if risk else "")
        self.evidence.setPlaceholderText("observed event categories that count for this risk, e.g. data breach")
        for label, w in (("Name", self.name), ("Organisation", self.unit), ("Risk type", self.category),
                         ("Risk owner", self.owner), ("Evidence categories", self.evidence),
                         ("Description", self.description)):
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

        self.has_outage = QCheckBox("An event takes services down (costs us and our customers per hour)")
        self.layout_.addWidget(self.has_outage)
        self.outage_box = QGroupBox("Third parties and outage")
        lay3 = QVBoxLayout(self.outage_box)
        self.outage = ImpactEditor(caption="Outage duration", hours=True)
        self.res_outage = ImpactEditor(caption="After controls", hours=True)
        lay3.addWidget(self.outage)
        lay3.addWidget(self.res_outage)
        tp = QFormLayout()
        self.services = _check_list(svc.list_services(), set(risk.service_ids) if risk else set(), height=80)
        self.suppliers = _check_list(svc.list_suppliers(), set(risk.supplier_ids) if risk else set(), height=80)
        tp.addRow("Disrupts services", self.services)
        tp.addRow("Comes from suppliers", self.suppliers)
        self.objectives = _check_list(svc.list_objectives(), set(risk.objective_ids) if risk else set(), height=70)
        tp.addRow("Threatens objectives", self.objectives)
        lay3.addLayout(tp)
        self.layout_.addWidget(self.outage_box)

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
        if risk and (risk.outage_hours or risk.residual_outage_hours):
            self.has_outage.setChecked(True)
            self.outage.set_spec(risk.outage_hours or risk.residual_outage_hours)
            self.res_outage.set_spec(risk.residual_outage_hours or risk.outage_hours)
        for editor in (self.outage, self.res_outage):
            editor.setEnabled(self.has_outage.isChecked())
        self.has_outage.toggled.connect(lambda on: (self.outage.setEnabled(on), self.res_outage.setEnabled(on),
                                                    self._update_preview()))
        for editor in (self.outage, self.res_outage):
            editor.changed.connect(self._update_preview)
        self.residual_box.setVisible(self.has_residual.isChecked())
        self.has_residual.toggled.connect(lambda on: (self.residual_box.setVisible(on), self._update_preview(),
                                                      self.adjustSize()))
        for editor in (self.freq, self.impact, self.res_freq, self.res_impact):
            editor.changed.connect(self._update_preview)
        self._update_preview()
        self.finish()

    def _score_text(self, label: str, freq: Dict[str, Any], impact: Dict[str, Any]) -> str:
        try:
            if self.has_outage.isChecked():  # incl. what the outage costs the linked services and customers
                v = self.values()
                probe = Risk(name="preview", business_unit_id=v["business_unit_id"] or "", frequency=freq,
                             impact=impact, service_ids=v["service_ids"], supplier_ids=v["supplier_ids"],
                             outage_hours=self.res_outage.spec() if label == "Residual" else self.outage.spec())
                impact = self.svc.risk_severity(probe, False)
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
            "service_ids": _checked(self.services),
            "supplier_ids": _checked(self.suppliers), "evidence_categories": _split(self.evidence.text()),
            "objective_ids": _checked(self.objectives),
            "outage_hours": self.outage.spec() if self.has_outage.isChecked() else None,
            "residual_outage_hours": (self.res_outage.spec() if self.has_outage.isChecked()
                                      and self.res_outage.spec() != self.outage.spec() else None),
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
        c = control
        self.impl_cost = QLineEdit("" if not c or c.implementation_cost is None else f"{c.implementation_cost:g}")
        self.impl_cost.setPlaceholderText("one-off")
        self.annual_cost = QLineEdit("" if not c or c.annual_cost is None else f"{c.annual_cost:g}")
        self.annual_cost.setPlaceholderText("per year: licences, staff, service")
        self.lifetime = QDoubleSpinBox()
        self.lifetime.setRange(0.5, 30)
        self.lifetime.setValue(c.lifetime_years if c else 3.0)
        self.lifetime.setSuffix(" years")
        self.freq_red = QSpinBox()
        self.freq_red.setRange(0, 99)
        self.freq_red.setSuffix(" %")
        self.freq_red.setValue(round((c.frequency_reduction or 0) * 100) if c else 0)
        self.freq_red.setToolTip("Share of the events it prevents")
        self.impact_red = QSpinBox()
        self.impact_red.setRange(0, 99)
        self.impact_red.setSuffix(" %")
        self.impact_red.setValue(round((c.impact_reduction or 0) * 100) if c else 0)
        self.impact_red.setToolTip("Share of the loss per event it removes (also shortens outages)")
        for label, w in (("Name", self.name), ("Organisation", self.unit), ("Type", self.kind),
                         ("Implementation", self.status), ("Effectiveness (1-5)", self.rating),
                         ("Owner", self.owner), ("Description", self.description),
                         ("Mitigates these risks", self.risks), ("Implementation cost", self.impl_cost),
                         ("Annual cost", self.annual_cost), ("Lifetime", self.lifetime),
                         ("Prevents events", self.freq_red), ("Reduces loss per event", self.impact_red)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        risk_ids = [self.risks.item(i).data(Qt.UserRole) for i in range(self.risks.count())
                    if self.risks.item(i).checkState() == Qt.Checked]
        return {"name": self.name.text(), "business_unit_id": self.unit.currentData(),
                "control_type": self.kind.currentData(), "implementation_status": self.status.currentData(),
                "effectiveness_rating": self.rating.value() or None, "owner_id": self.owner.currentData(),
                "description": self.description.toPlainText().strip() or None, "risk_ids": risk_ids,
                "implementation_cost": _float(self.impl_cost), "annual_cost": _float(self.annual_cost),
                "lifetime_years": self.lifetime.value(),
                "frequency_reduction": self.freq_red.value() / 100 or None,
                "impact_reduction": self.impact_red.value() / 100 or None}



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
        self.lei = QLineEdit((s.lei or "") if s else "")
        self.company_number = QLineEdit((s.company_number or "") if s else "")
        self.company_number.setPlaceholderText("e.g. CVR")
        self.parent_company = QLineEdit((s.parent_company or "") if s else "")
        self.description = QPlainTextEdit((s.description or "") if s else "")
        self.description.setFixedHeight(50)
        for label, w in (("Name", self.name), ("Tier", self.tier), ("Country", self.country),
                         ("Relationship owner", self.owner), ("Contract start", self.contract_start),
                         ("Contract end", self.contract_end), ("Next review", self.next_review),
                         ("", self.personal), ("Data locations", self.locations),
                         ("Certifications / reports", self.certs), ("", self.exit_plan),
                         ("Its subcontractors", self.subs), ("LEI", self.lei), ("Company number", self.company_number),
                         ("Ultimate parent", self.parent_company), ("Description", self.description)):
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
                "subcontractor_ids": _checked(self.subs), "lei": self.lei.text().strip() or None,
                "company_number": self.company_number.text().strip() or None,
                "parent_company": self.parent_company.text().strip() or None,
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
        self.cost = QLineEdit("" if not s or s.outage_cost_per_hour is None else f"{s.outage_cost_per_hour:g}")
        self.cost.setPlaceholderText("our own cost per hour while it is down")
        self.suppliers = _check_list(svc.list_suppliers(), set(s.supplier_ids) if s else set())
        self.assets = _check_list(svc.list_assets(), set(s.asset_ids) if s else set(), height=90)
        self.description = QPlainTextEdit((s.description or "") if s else "")
        self.description.setFixedHeight(50)
        self.ict_type = QLineEdit((s.ict_service_type or "") if s else "")
        self.ict_type.setPlaceholderText("e.g. Software as a service (SaaS)")
        self.locations = QLineEdit(_csv(s.data_locations) if s else "")
        self.locations.setPlaceholderText("where its data is stored and processed, e.g. DK, DE")
        for label, w in (("Name", self.name), ("Delivered by", self.unit), ("Criticality", self.criticality),
                         ("ICT service type", self.ict_type), ("Data locations", self.locations),
                         ("Owner", self.owner), ("Recovery time (RTO)", self.rto),
                         ("Recovery point (RPO)", self.rpo), ("Outage cost per hour", self.cost),
                         ("Depends on suppliers", self.suppliers),
                         ("Runs on assets", self.assets), ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "business_unit_id": self.unit.currentData(),
                "criticality": self.criticality.currentData(), "owner_id": self.owner.currentData(),
                "rto_hours": _float(self.rto), "rpo_hours": _float(self.rpo),
                "outage_cost_per_hour": _float(self.cost),
                "supplier_ids": _checked(self.suppliers), "asset_ids": _checked(self.assets),
                "ict_service_type": self.ict_type.text().strip() or None,
                "data_locations": _split(self.locations.text()),
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
        self.penalty = QLineEdit("" if not c or c.penalty_per_hour is None else f"{c.penalty_per_hour:g}")
        self.penalty.setPlaceholderText("per hour of outage")
        self.grace = _hours(c.penalty_grace_hours if c else None)
        self.grace.setPlaceholderText("hours before the penalty starts")
        self.cap = QLineEdit("" if not c or c.penalty_cap is None else f"{c.penalty_cap:g}")
        self.cap.setPlaceholderText("maximum per outage (empty = no cap)")
        for label, w in (("Name", self.name), ("Sector", self.sector), ("Regulation", self.regulations),
                         ("", self.critical), ("Uses services", self.services), ("Account owner", self.owner),
                         ("Contract value", self.value), ("Contract end", self.contract_end),
                         ("Notify incidents within", self.notify), ("Contract grants", self.audit),
                         ("", self.exit), ("Penalty", self.penalty), ("Penalty after", self.grace),
                         ("Penalty cap", self.cap), ("Notes", self.notes)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "sector": self.sector.currentText().strip() or None,
                "regulations": _checked(self.regulations), "critical_function": self.critical.isChecked(),
                "service_ids": _checked(self.services), "account_owner_id": self.owner.currentData(),
                "contract_value": _float(self.value), "contract_end": self.contract_end.text().strip() or None,
                "notification_hours": _float(self.notify), "audit_rights": self.audit.isChecked(),
                "exit_support": self.exit.isChecked(), "notes": self.notes.toPlainText().strip() or None,
                "penalty_per_hour": _float(self.penalty), "penalty_grace_hours": _float(self.grace),
                "penalty_cap": _float(self.cap)}


def _risk_combo(svc: RiskService, selected: Optional[str]) -> QComboBox:
    combo = QComboBox()
    for r in sorted(svc.list_risks(), key=lambda r: r.name):
        combo.addItem(r.name, r.id)
    if selected:
        combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


class CorrelationDialog(BaseDialog):
    """Two risks and how strongly their bad years come together."""

    def __init__(self, svc: RiskService, corr: Optional[Correlation] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit correlation" if corr else "New correlation", corr is not None, parent)
        self.risk_a = _risk_combo(svc, corr.risk_a_id if corr else None)
        self.risk_b = _risk_combo(svc, corr.risk_b_id if corr else None)
        if not corr and self.risk_b.count() > 1:
            self.risk_b.setCurrentIndex(1)
        self.coefficient = QDoubleSpinBox()
        self.coefficient.setRange(-0.99, 0.99)
        self.coefficient.setSingleStep(0.05)
        self.coefficient.setDecimals(2)
        self.coefficient.setValue(corr.coefficient if corr else 0.3)
        hint = QLabel("0.1-0.3 weak, 0.3-0.6 moderate, above 0.6 strong. Negative: one tends to be good "
                      "when the other is bad. Applied by the Monte Carlo engine.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b")
        self.rationale = QPlainTextEdit((corr.rationale or "") if corr else "")
        self.rationale.setPlaceholderText("Why do they move together? e.g. same attackers, shared platform")
        self.rationale.setFixedHeight(60)
        for label, w in (("Risk A", self.risk_a), ("Risk B", self.risk_b), ("Coefficient", self.coefficient),
                         ("", hint), ("Rationale", self.rationale)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"risk_a_id": self.risk_a.currentData(), "risk_b_id": self.risk_b.currentData(),
                "coefficient": round(self.coefficient.value(), 2),
                "rationale": self.rationale.toPlainText().strip() or None}


class AppetiteDialog(BaseDialog):
    """A tolerance curve: at most this chance per year of losing more than this amount."""

    def __init__(self, svc: RiskService, appetite: Optional[Appetite] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit risk appetite" if appetite else "New risk appetite", appetite is not None, parent)
        a = appetite
        self.name = QLineEdit(a.name if a else "")
        self.unit = QComboBox()
        self.unit.addItem("The whole organisation", None)
        for u in svc.list_business_units():
            self.unit.addItem(u.name, u.id)
        if a and a.business_unit_id:
            self.unit.setCurrentIndex(max(0, self.unit.findData(a.business_unit_id)))
        self.owner = _person_combo(svc, a.owner_id if a else None)
        self.points = QTableWidget(0, 2)
        self.points.setHorizontalHeaderLabels(["Annual loss above", "At most chance per year %"])
        self.points.horizontalHeader().setStretchLastSection(True)
        self.points.setColumnWidth(0, 180)
        self.points.setFixedHeight(150)
        for p in (a.points if a else [{"loss": 1_000_000, "probability": 0.1}, {"loss": 10_000_000, "probability": 0.01}]):
            self._add_point(p["loss"], p["probability"] * 100)
        add, remove = QPushButton("Add point"), QPushButton("Remove selected")
        add.clicked.connect(lambda: self._add_point(None, None))
        remove.clicked.connect(lambda: self.points.removeRow(self.points.currentRow()))
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.points)
        bl.addLayout(buttons)
        self.statement = QPlainTextEdit((a.statement or "") if a else "")
        self.statement.setPlaceholderText("e.g. Losing more than 10m should happen at most once in 100 years")
        self.statement.setFixedHeight(60)
        for label, w in (("Name", self.name), ("Applies to", self.unit), ("Owner", self.owner),
                         ("Tolerance", box), ("Statement", self.statement)):
            self.form.addRow(label, w)
        self.finish()

    def _add_point(self, loss, percent) -> None:
        r = self.points.rowCount()
        self.points.insertRow(r)
        self.points.setItem(r, 0, QTableWidgetItem("" if loss is None else f"{loss:,.0f}"))
        self.points.setItem(r, 1, QTableWidgetItem("" if percent is None else f"{percent:g}"))

    def values(self) -> Dict[str, Any]:
        points = []
        for r in range(self.points.rowCount()):
            cells = [self.points.item(r, c).text().strip() if self.points.item(r, c) else "" for c in (0, 1)]
            if not any(cells):
                continue
            try:
                loss = float(cells[0].replace(",", "").replace(" ", ""))
                prob = float(cells[1].replace("%", "").replace(",", ".")) / 100
            except ValueError:
                raise ValueError(f"Tolerance row {r + 1}: enter an amount and a percentage") from None
            points.append({"loss": loss, "probability": prob})
        return {"name": self.name.text(), "business_unit_id": self.unit.currentData(),
                "owner_id": self.owner.currentData(), "points": points,
                "statement": self.statement.toPlainText().strip() or None}


class IncidentDialog(BaseDialog):
    """An incident: when we became aware of it, what it hits and whether it is significant under NIS2."""

    def __init__(self, svc: RiskService, incident: Optional[Incident] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit incident" if incident else "New incident", incident is not None, parent)
        i = incident
        from datetime import datetime
        self.title_ = QLineEdit(i.title if i else "")
        self.detected = QLineEdit(i.detected_at.replace("T", " ") if i else datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.detected.setPlaceholderText("YYYY-MM-DD HH:MM - when we became aware (the deadlines run from here)")
        self.status = _enum_combo(INCIDENT_STATUSES, i.status if i else "OPEN")
        self.nis2 = QComboBox()
        for text, value in (("Not assessed yet", None), ("Yes - significant (reporting deadlines apply)", True),
                            ("No - not significant", False)):
            self.nis2.addItem(text, value)
        self.nis2.setCurrentIndex(max(0, self.nis2.findData(i.nis2_significant if i else None)))
        self.services = _check_list(svc.list_services(), set(i.service_ids) if i else set(), height=90)
        self.suppliers = _check_list(svc.list_suppliers(), set(i.supplier_ids) if i else set(), height=80)
        self.risk = QComboBox()
        self.risk.addItem("(none)", None)
        for r in sorted(svc.list_risks(), key=lambda r: r.name):
            self.risk.addItem(r.name, r.id)
        if i and i.risk_id:
            self.risk.setCurrentIndex(max(0, self.risk.findData(i.risk_id)))
        self.owner = _person_combo(svc, i.owner_id if i else None)
        self.resolved = QLineEdit((i.resolved_at or "").replace("T", " ") if i else "")
        self.resolved.setPlaceholderText("YYYY-MM-DD HH:MM")
        self.impact = QLineEdit("" if not i or i.financial_impact is None else f"{i.financial_impact:g}")
        self.root_cause = QLineEdit((i.root_cause or "") if i else "")
        self.description = QPlainTextEdit((i.description or "") if i else "")
        self.description.setFixedHeight(50)
        self._notifications = dict(i.notifications) if i else {}
        for label, w in (("Title", self.title_), ("Detected", self.detected), ("Status", self.status),
                         ("NIS2 significant", self.nis2), ("Services affected", self.services),
                         ("Started at supplier", self.suppliers), ("Risk that materialised", self.risk),
                         ("Handled by", self.owner), ("Resolved", self.resolved), ("Financial impact", self.impact),
                         ("Root cause", self.root_cause), ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"title": self.title_.text(), "detected_at": self.detected.text().strip(),
                "status": self.status.currentData(), "nis2_significant": self.nis2.currentData(),
                "service_ids": _checked(self.services), "supplier_ids": _checked(self.suppliers),
                "risk_id": self.risk.currentData(), "owner_id": self.owner.currentData(),
                "resolved_at": self.resolved.text().strip() or None, "financial_impact": _float(self.impact),
                "root_cause": self.root_cause.text().strip() or None,
                "description": self.description.toPlainText().strip() or None}


def _category_combo(svc: RiskService, value: str) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItems(svc.evidence_categories())
    combo.setCurrentText(value)
    return combo


class ObservationDialog(BaseDialog):
    """An event that happened (or a count of them), at group, unit or supplier level."""

    def __init__(self, svc: RiskService, obs: Optional[Observation] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit observation" if obs else "New observation", obs is not None, parent)
        from datetime import date
        o = obs
        self.date = QLineEdit(o.date if o else date.today().isoformat())
        self.date.setPlaceholderText("YYYY-MM-DD")
        self.category = _category_combo(svc, o.category if o else "")
        self.count = QSpinBox()
        self.count.setRange(0, 1_000_000)
        self.count.setValue(o.count if o else 1)
        self.loss = QLineEdit("" if not o or o.loss is None else f"{o.loss:g}")
        self.loss.setPlaceholderText("total loss of these events, if known")
        self.level = QComboBox()
        self.level.addItem("The whole group", "group:")
        for u in svc.list_business_units():
            self.level.addItem(f"Unit: {u.name}", f"unit:{u.id}")
        for s in svc.list_suppliers():
            self.level.addItem(f"Supplier: {s.name}", f"supplier:{s.id}")
        if o and o.business_unit_id:
            self.level.setCurrentIndex(max(0, self.level.findData(f"unit:{o.business_unit_id}")))
        elif o and o.supplier_id:
            self.level.setCurrentIndex(max(0, self.level.findData(f"supplier:{o.supplier_id}")))
        self.risk = QComboBox()
        self.risk.addItem("(by category)", None)
        for r in sorted(svc.list_risks(), key=lambda r: r.name):
            self.risk.addItem(r.name, r.id)
        if o and o.risk_id:
            self.risk.setCurrentIndex(max(0, self.risk.findData(o.risk_id)))
        self.source = QLineEdit((o.source or "") if o else "")
        self.source.setPlaceholderText("e.g. incident log - the period counts from its first entry")
        self.description = QLineEdit((o.description or "") if o else "")
        for label, w in (("Date", self.date), ("Category", self.category), ("Count", self.count), ("Loss", self.loss),
                         ("Level", self.level), ("Risk", self.risk), ("Source", self.source),
                         ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        level, ref = self.level.currentData().split(":", 1)
        return {"date": self.date.text().strip(), "category": self.category.currentText().strip(),
                "count": self.count.value(), "loss": _float(self.loss),
                "business_unit_id": ref if level == "unit" else None,
                "supplier_id": ref if level == "supplier" else None, "risk_id": self.risk.currentData(),
                "source": self.source.text().strip() or None, "description": self.description.text().strip() or None}


class BenchmarkDialog(BaseDialog):
    """A public figure for organisations like ours."""

    def __init__(self, svc: RiskService, bm: Optional[Benchmark] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit benchmark" if bm else "New benchmark", bm is not None, parent)
        b = bm
        self.name = QLineEdit(b.name if b else "")
        self.category = _category_combo(svc, b.category if b else "")
        self.rate = QLineEdit("" if not b or b.events_per_year is None else f"{b.events_per_year:g}")
        self.rate.setPlaceholderText("for an organisation like ours, e.g. 0.1 = once in 10 years")
        self.p10 = QLineEdit("" if not b or b.loss_p10 is None else f"{b.loss_p10:g}")
        self.p90 = QLineEdit("" if not b or b.loss_p90 is None else f"{b.loss_p90:g}")
        self.weight = QDoubleSpinBox()
        self.weight.setRange(0.1, 100)
        self.weight.setValue(b.weight if b else 1.0)
        self.weight.setToolTip("Years of our own data (frequency) and events (loss) it is worth")
        self.year = QSpinBox()
        self.year.setRange(0, 2100)
        self.year.setSpecialValueText("-")
        self.year.setValue((b.year or 0) if b else 0)
        self.source = QLineEdit((b.source or "") if b else "")
        self.source.setPlaceholderText("citation or URL")
        self.notes = QLineEdit((b.notes or "") if b else "")
        for label, w in (("Name", self.name), ("Category", self.category), ("Events per year", self.rate),
                         ("Loss per event, low P10", self.p10), ("Loss per event, high P90", self.p90),
                         ("Weight", self.weight), ("Year", self.year), ("Source", self.source), ("Notes", self.notes)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text(), "category": self.category.currentText().strip(),
                "events_per_year": _float(self.rate), "loss_p10": _float(self.p10), "loss_p90": _float(self.p90),
                "weight": self.weight.value(), "year": self.year.value() or None,
                "source": self.source.text().strip() or None, "notes": self.notes.text().strip() or None}


class ObjectiveDialog(BaseDialog):
    """Something we must achieve, the services it is delivered through and how much loss is tolerable."""

    def __init__(self, svc: RiskService, obj: Optional[Objective] = None, parent: Optional[QWidget] = None):
        super().__init__("Edit objective" if obj else "New objective", obj is not None, parent)
        o = obj
        self.name = QLineEdit(o.name if o else "")
        self.criticality = _enum_combo(OBJECTIVE_CRITICALITIES, o.criticality if o else "IMPORTANT")
        self.unit = QComboBox()
        self.unit.addItem("The whole organisation", None)
        for u in svc.list_business_units():
            self.unit.addItem(u.name, u.id)
        if o and o.business_unit_id:
            self.unit.setCurrentIndex(max(0, self.unit.findData(o.business_unit_id)))
        self.owner = _person_combo(svc, o.owner_id if o else None)
        self.services = _check_list(svc.list_services(), set(o.service_ids) if o else set(), height=90)
        self.kpi = QLineEdit((o.kpi or "") if o else "")
        self.kpi.setPlaceholderText("how achievement is measured, e.g. availability at least 99.9%")
        self.target = QLineEdit((o.target_date or "") if o else "")
        self.target.setPlaceholderText("YYYY-MM-DD")
        self.tol_loss = QLineEdit("" if not o or o.tolerance_loss is None else f"{o.tolerance_loss:g}")
        self.tol_loss.setPlaceholderText("annual loss against this objective")
        self.tol_prob = QLineEdit("" if not o or o.tolerance_probability is None else f"{o.tolerance_probability * 100:g}")
        self.tol_prob.setPlaceholderText("at most this chance per year, in %")
        self.description = QPlainTextEdit((o.description or "") if o else "")
        self.description.setFixedHeight(50)
        for label, w in (("Name", self.name), ("Criticality", self.criticality), ("Pursued by", self.unit),
                         ("Owner", self.owner), ("Delivered through services", self.services), ("KPI", self.kpi),
                         ("Target date", self.target), ("Tolerance: loss above", self.tol_loss),
                         ("Tolerance: max chance %", self.tol_prob), ("Description", self.description)):
            self.form.addRow(label, w)
        self.finish()

    def values(self) -> Dict[str, Any]:
        prob = _float(self.tol_prob)
        return {"name": self.name.text(), "criticality": self.criticality.currentData(),
                "business_unit_id": self.unit.currentData(), "owner_id": self.owner.currentData(),
                "service_ids": _checked(self.services), "kpi": self.kpi.text().strip() or None,
                "target_date": self.target.text().strip() or None, "tolerance_loss": _float(self.tol_loss),
                "tolerance_probability": None if prob is None else prob / 100,
                "description": self.description.toPlainText().strip() or None}


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
