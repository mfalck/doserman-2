"""Request/response models for the REST API (the contract web front ends use)."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Spec = Dict[str, Any]
OrgType = Literal["COMPANY", "DEPARTMENT", "SUBSIDIARY", "SUPPLIER"]
ControlType = Literal["PREVENTIVE", "DETECTIVE", "CORRECTIVE", "DIRECTIVE"]
ImplStatus = Literal["PLANNED", "IN_PROGRESS", "IMPLEMENTED", "RETIRED"]
RiskEventType = Literal["INCIDENT", "NEAR_MISS", "AUDIT_FINDING"]
RiskEventStatus = Literal["OPEN", "INVESTIGATING", "CLOSED"]
ControlEventType = Literal["TEST", "REVIEW", "AUDIT"]
ControlEventStatus = Literal["PASSED", "FAILED", "PARTIAL", "IN_PROGRESS"]

FREQUENCY_EXAMPLE = {"type": "poisson", "rate": 0.5}
IMPACT_EXAMPLE = {"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 1_000_000}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# -- Business units ---------------------------------------------------------

class BusinessUnitCreate(_In):
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    code: Optional[str] = None
    organization_type: OrgType = "DEPARTMENT"
    lei: Optional[str] = None
    country: Optional[str] = None


class BusinessUnitUpdate(_In):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    code: Optional[str] = None
    organization_type: Optional[OrgType] = None
    risk_owner_id: Optional[str] = None
    delegate_ids: Optional[List[str]] = None
    lei: Optional[str] = None
    country: Optional[str] = None


class BusinessUnitOut(_Out):
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    code: Optional[str] = None
    organization_type: str
    risk_owner_id: Optional[str] = None
    delegate_ids: List[str] = []
    lei: Optional[str] = None
    country: Optional[str] = None


class RiskOwnerAssign(_In):
    person_id: Optional[str] = Field(None, description="null clears the risk owner")


# -- Assets -----------------------------------------------------------------

class AssetCreate(_In):
    name: str
    asset_type: str
    business_unit_id: str
    description: Optional[str] = None
    criticality: Optional[str] = None
    value: Optional[float] = Field(None, ge=0)


class AssetUpdate(_In):
    name: Optional[str] = None
    asset_type: Optional[str] = None
    business_unit_id: Optional[str] = None
    description: Optional[str] = None
    criticality: Optional[str] = None
    value: Optional[float] = Field(None, ge=0)


class AssetOut(_Out):
    name: str
    asset_type: str
    business_unit_id: str
    description: Optional[str] = None
    criticality: Optional[str] = None
    value: Optional[float] = None


# -- Persons ----------------------------------------------------------------

class PersonCreate(_In):
    name: str
    title: Optional[str] = None
    email: Optional[str] = None


class PersonUpdate(_In):
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None


class PersonOut(_Out):
    name: str
    title: Optional[str] = None
    email: Optional[str] = None


# -- Risks ------------------------------------------------------------------

class RiskCreate(_In):
    name: str
    business_unit_id: str
    frequency: Spec = Field(..., examples=[FREQUENCY_EXAMPLE])
    impact: Spec = Field(..., examples=[IMPACT_EXAMPLE])
    description: Optional[str] = None
    category: Optional[str] = None
    asset_ids: List[str] = []
    owner_id: Optional[str] = None
    residual_frequency: Optional[Spec] = None
    residual_impact: Optional[Spec] = None
    service_ids: List[str] = []
    supplier_ids: List[str] = []
    outage_hours: Optional[Spec] = Field(None, examples=[{"dist": "lognorm_p10_p90", "p10": 1, "p90": 24}],
                                         description="duration of one event's outage, in hours")
    residual_outage_hours: Optional[Spec] = None


class RiskUpdate(_In):
    name: Optional[str] = None
    business_unit_id: Optional[str] = None
    frequency: Optional[Spec] = None
    impact: Optional[Spec] = None
    description: Optional[str] = None
    category: Optional[str] = None
    asset_ids: Optional[List[str]] = None
    owner_id: Optional[str] = None
    residual_frequency: Optional[Spec] = None
    residual_impact: Optional[Spec] = None
    service_ids: Optional[List[str]] = None
    supplier_ids: Optional[List[str]] = None
    outage_hours: Optional[Spec] = None
    residual_outage_hours: Optional[Spec] = None


class ScoreOut(BaseModel):
    likelihood: int = Field(..., description="1-5, from P(at least one event per year)")
    impact: int = Field(..., description="1-5, from the median loss per event")
    score: int = Field(..., description="likelihood x impact, 1-25")
    level: str
    annual_probability: float
    typical_event_loss: float
    expected_annual_loss: float


class RiskScoresOut(BaseModel):
    inherent: ScoreOut
    residual: ScoreOut


class RiskOut(_Out):
    name: str
    business_unit_id: str
    frequency: Spec
    impact: Spec
    description: Optional[str] = None
    category: Optional[str] = None
    asset_ids: List[str] = []
    owner_id: Optional[str] = None
    residual_frequency: Optional[Spec] = None
    residual_impact: Optional[Spec] = None
    service_ids: List[str] = []
    supplier_ids: List[str] = []
    outage_hours: Optional[Spec] = None
    residual_outage_hours: Optional[Spec] = None
    scores: RiskScoresOut
    control_ids: List[str] = []


# -- Controls ---------------------------------------------------------------

class ControlCreate(_In):
    name: str
    business_unit_id: str
    description: Optional[str] = None
    control_type: ControlType = "PREVENTIVE"
    implementation_status: ImplStatus = "PLANNED"
    effectiveness_rating: Optional[int] = Field(None, ge=1, le=5)
    owner_id: Optional[str] = None
    risk_ids: List[str] = []


class ControlUpdate(_In):
    name: Optional[str] = None
    business_unit_id: Optional[str] = None
    description: Optional[str] = None
    control_type: Optional[ControlType] = None
    implementation_status: Optional[ImplStatus] = None
    effectiveness_rating: Optional[int] = Field(None, ge=1, le=5)
    owner_id: Optional[str] = None
    risk_ids: Optional[List[str]] = None


class ControlOut(_Out):
    name: str
    business_unit_id: str
    description: Optional[str] = None
    control_type: str
    implementation_status: str
    effectiveness_rating: Optional[int] = None
    owner_id: Optional[str] = None
    risk_ids: List[str] = []


# -- Third parties: suppliers, services, customers --------------------------------

Tier = Literal["CRITICAL", "IMPORTANT", "STANDARD"]
Criticality = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
Regulation = Literal["NIS2_ESSENTIAL", "NIS2_IMPORTANT", "DORA", "CER", "GDPR", "OTHER"]


class SupplierCreate(_In):
    name: str
    tier: Tier = "STANDARD"
    description: Optional[str] = None
    country: Optional[str] = None
    owner_id: Optional[str] = None
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    next_review: Optional[str] = None
    processes_personal_data: bool = False
    data_locations: List[str] = []
    certifications: List[str] = []
    exit_plan: bool = False
    subcontractor_ids: List[str] = []
    lei: Optional[str] = None
    company_number: Optional[str] = None
    parent_company: Optional[str] = None


class SupplierUpdate(_In):
    name: Optional[str] = None
    tier: Optional[Tier] = None
    description: Optional[str] = None
    country: Optional[str] = None
    owner_id: Optional[str] = None
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    next_review: Optional[str] = None
    processes_personal_data: Optional[bool] = None
    data_locations: Optional[List[str]] = None
    certifications: Optional[List[str]] = None
    exit_plan: Optional[bool] = None
    subcontractor_ids: Optional[List[str]] = None
    lei: Optional[str] = None
    company_number: Optional[str] = None
    parent_company: Optional[str] = None


class SupplierOut(_Out):
    name: str
    tier: str
    description: Optional[str] = None
    country: Optional[str] = None
    owner_id: Optional[str] = None
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    next_review: Optional[str] = None
    processes_personal_data: bool = False
    data_locations: List[str] = []
    certifications: List[str] = []
    exit_plan: bool = False
    subcontractor_ids: List[str] = []
    lei: Optional[str] = None
    company_number: Optional[str] = None
    parent_company: Optional[str] = None


IncidentStatus = Literal["OPEN", "CONTAINED", "RESOLVED", "CLOSED"]


class IncidentCreate(_In):
    title: str
    detected_at: str = Field(..., description="When we became aware, local time YYYY-MM-DDTHH:MM")
    status: IncidentStatus = "OPEN"
    description: Optional[str] = None
    service_ids: List[str] = []
    supplier_ids: List[str] = []
    risk_id: Optional[str] = None
    owner_id: Optional[str] = None
    nis2_significant: Optional[bool] = None
    resolved_at: Optional[str] = None
    root_cause: Optional[str] = None
    financial_impact: Optional[float] = Field(None, ge=0)
    notifications: Dict[str, str] = {}


class IncidentUpdate(_In):
    title: Optional[str] = None
    detected_at: Optional[str] = None
    status: Optional[IncidentStatus] = None
    description: Optional[str] = None
    service_ids: Optional[List[str]] = None
    supplier_ids: Optional[List[str]] = None
    risk_id: Optional[str] = None
    owner_id: Optional[str] = None
    nis2_significant: Optional[bool] = None
    resolved_at: Optional[str] = None
    root_cause: Optional[str] = None
    financial_impact: Optional[float] = Field(None, ge=0)
    notifications: Optional[Dict[str, str]] = None


class IncidentOut(_Out):
    title: str
    detected_at: str
    status: str
    description: Optional[str] = None
    service_ids: List[str] = []
    supplier_ids: List[str] = []
    risk_id: Optional[str] = None
    owner_id: Optional[str] = None
    nis2_significant: Optional[bool] = None
    resolved_at: Optional[str] = None
    root_cause: Optional[str] = None
    financial_impact: Optional[float] = None
    notifications: Dict[str, str] = {}


class NotificationSent(_In):
    key: str = Field(..., description="e.g. nis2:early_warning or customer:<id>")
    sent_at: Optional[str] = Field(None, description="default now; empty string undoes it")


class CorrelationCreate(_In):
    risk_a_id: str
    risk_b_id: str
    coefficient: float = Field(..., ge=-0.99, le=0.99)
    rationale: Optional[str] = None


class CorrelationUpdate(_In):
    risk_a_id: Optional[str] = None
    risk_b_id: Optional[str] = None
    coefficient: Optional[float] = Field(None, ge=-0.99, le=0.99)
    rationale: Optional[str] = None


class CorrelationOut(_Out):
    risk_a_id: str
    risk_b_id: str
    coefficient: float
    rationale: Optional[str] = None


class AppetitePoint(BaseModel):
    loss: float = Field(..., gt=0)
    probability: float = Field(..., gt=0, lt=1)


class AppetiteCreate(_In):
    name: str
    points: List[AppetitePoint]
    business_unit_id: Optional[str] = None
    statement: Optional[str] = None
    owner_id: Optional[str] = None


class AppetiteUpdate(_In):
    name: Optional[str] = None
    points: Optional[List[AppetitePoint]] = None
    business_unit_id: Optional[str] = None
    statement: Optional[str] = None
    owner_id: Optional[str] = None


class AppetiteOut(_Out):
    name: str
    points: List[Dict[str, float]] = []
    business_unit_id: Optional[str] = None
    statement: Optional[str] = None
    owner_id: Optional[str] = None


class ServiceCreate(_In):
    name: str
    business_unit_id: str
    description: Optional[str] = None
    criticality: Criticality = "MEDIUM"
    owner_id: Optional[str] = None
    asset_ids: List[str] = []
    supplier_ids: List[str] = []
    rto_hours: Optional[float] = Field(None, ge=0)
    rpo_hours: Optional[float] = Field(None, ge=0)
    outage_cost_per_hour: Optional[float] = Field(None, ge=0)
    ict_service_type: Optional[str] = None
    data_locations: List[str] = []


class ServiceUpdate(_In):
    name: Optional[str] = None
    business_unit_id: Optional[str] = None
    description: Optional[str] = None
    criticality: Optional[Criticality] = None
    owner_id: Optional[str] = None
    asset_ids: Optional[List[str]] = None
    supplier_ids: Optional[List[str]] = None
    rto_hours: Optional[float] = Field(None, ge=0)
    rpo_hours: Optional[float] = Field(None, ge=0)
    outage_cost_per_hour: Optional[float] = Field(None, ge=0)
    ict_service_type: Optional[str] = None
    data_locations: Optional[List[str]] = None


class ServiceOut(_Out):
    name: str
    business_unit_id: str
    description: Optional[str] = None
    criticality: str
    owner_id: Optional[str] = None
    asset_ids: List[str] = []
    supplier_ids: List[str] = []
    rto_hours: Optional[float] = None
    rpo_hours: Optional[float] = None
    outage_cost_per_hour: Optional[float] = None
    ict_service_type: Optional[str] = None
    data_locations: List[str] = []


class CustomerCreate(_In):
    name: str
    sector: Optional[str] = None
    regulations: List[Regulation] = []
    service_ids: List[str] = []
    critical_function: bool = False
    account_owner_id: Optional[str] = None
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    contract_value: Optional[float] = Field(None, ge=0)
    notification_hours: Optional[float] = Field(None, ge=0)
    audit_rights: bool = False
    exit_support: bool = False
    notes: Optional[str] = None
    penalty_per_hour: Optional[float] = Field(None, ge=0)
    penalty_grace_hours: Optional[float] = Field(None, ge=0)
    penalty_cap: Optional[float] = Field(None, ge=0)


class CustomerUpdate(_In):
    name: Optional[str] = None
    sector: Optional[str] = None
    regulations: Optional[List[Regulation]] = None
    service_ids: Optional[List[str]] = None
    critical_function: Optional[bool] = None
    account_owner_id: Optional[str] = None
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    contract_value: Optional[float] = Field(None, ge=0)
    notification_hours: Optional[float] = Field(None, ge=0)
    audit_rights: Optional[bool] = None
    exit_support: Optional[bool] = None
    notes: Optional[str] = None
    penalty_per_hour: Optional[float] = Field(None, ge=0)
    penalty_grace_hours: Optional[float] = Field(None, ge=0)
    penalty_cap: Optional[float] = Field(None, ge=0)


class CustomerOut(_Out):
    name: str
    sector: Optional[str] = None
    regulations: List[str] = []
    service_ids: List[str] = []
    critical_function: bool = False
    account_owner_id: Optional[str] = None
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    contract_value: Optional[float] = None
    notification_hours: Optional[float] = None
    audit_rights: bool = False
    exit_support: bool = False
    notes: Optional[str] = None
    penalty_per_hour: Optional[float] = None
    penalty_grace_hours: Optional[float] = None
    penalty_cap: Optional[float] = None


# -- Events -----------------------------------------------------------------

class RiskEventCreate(_In):
    name: str
    risk_id: str
    date: str = Field(..., examples=["2026-11-24"])
    event_type: RiskEventType = "INCIDENT"
    status: RiskEventStatus = "OPEN"
    description: Optional[str] = None
    impact_description: Optional[str] = None
    financial_impact: Optional[float] = Field(None, ge=0)


class RiskEventUpdate(_In):
    name: Optional[str] = None
    risk_id: Optional[str] = None
    date: Optional[str] = None
    event_type: Optional[RiskEventType] = None
    status: Optional[RiskEventStatus] = None
    description: Optional[str] = None
    impact_description: Optional[str] = None
    financial_impact: Optional[float] = Field(None, ge=0)


class RiskEventOut(_Out):
    name: str
    risk_id: str
    date: str
    event_type: str
    status: str
    description: Optional[str] = None
    impact_description: Optional[str] = None
    financial_impact: Optional[float] = None


class ControlEventCreate(_In):
    control_id: str
    date: str = Field(..., examples=["2026-11-24"])
    event_type: ControlEventType = "TEST"
    status: ControlEventStatus = "IN_PROGRESS"
    description: Optional[str] = None


class ControlEventUpdate(_In):
    control_id: Optional[str] = None
    date: Optional[str] = None
    event_type: Optional[ControlEventType] = None
    status: Optional[ControlEventStatus] = None
    description: Optional[str] = None


class ControlEventOut(_Out):
    control_id: str
    date: str
    event_type: str
    status: str
    description: Optional[str] = None


# -- Audit ------------------------------------------------------------------

class AuditEntryOut(_Out):
    seq: int
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    details: Optional[str] = None
    user_id: str
    timestamp: str
    source: str
    reason: Optional[str] = None
    changes: Optional[Dict[str, Any]] = Field(None, description="{field: [before, after]}")
    state_hash: Optional[str] = None
    prev_hash: str
    hash: str


# -- Simulation -------------------------------------------------------------

class SimulationRequest(_In):
    engine: Literal["analytic", "monte_carlo"] = Field(
        "analytic", description="analytic = exact distributions via FFT; monte_carlo = simulation")
    n_trials: int = Field(10_000, ge=100, le=1_000_000, description="Monte Carlo only")
    seed: Optional[int] = Field(None, description="Monte Carlo only")
    residual: bool = True
    unit_id: Optional[str] = Field(None, description="Only risks in this unit and its children")
    cache: bool = Field(True, description="Reuse a stored run with the same data and settings")
    label: Optional[str] = Field(None, max_length=200, description="Name the run, e.g. 'Board meeting'")


class RunLabel(_In):
    label: Optional[str] = Field(None, max_length=200)


# -- Users and login --------------------------------------------------------------

Role = Literal["viewer", "editor", "admin"]


class LoginRequest(_In):
    username: str
    password: str


class PasswordChange(_In):
    current_password: str
    new_password: str = Field(..., min_length=10)


class PasswordSet(_In):
    password: str = Field(..., min_length=10)


class UserCreate(_In):
    username: str
    role: Role = "viewer"
    password: str = Field(..., min_length=10)
    display_name: Optional[str] = None
    email: Optional[str] = None


class UserUpdate(_In):
    role: Optional[Role] = None
    display_name: Optional[str] = None
    email: Optional[str] = None
    active: Optional[bool] = None


class PeriodClose(_In):
    note: Optional[str] = Field(None, max_length=1000)


class AnchorCreate(_In):
    reason: Optional[str] = Field(None, max_length=500)


class TokenCreate(_In):
    name: str = Field(..., min_length=1, max_length=100)
    days: Optional[int] = Field(None, ge=1, le=3650, description="None = does not expire")
