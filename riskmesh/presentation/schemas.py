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


class BusinessUnitUpdate(_In):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    code: Optional[str] = None
    organization_type: Optional[OrgType] = None
    risk_owner_id: Optional[str] = None
    delegate_ids: Optional[List[str]] = None


class BusinessUnitOut(_Out):
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    code: Optional[str] = None
    organization_type: str
    risk_owner_id: Optional[str] = None
    delegate_ids: List[str] = []


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
