"""Request/response models for the REST API (the contract the web front end uses)."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

Spec = Dict[str, Any]

FREQUENCY_EXAMPLE = {"type": "poisson", "rate": 0.5}
IMPACT_EXAMPLE = {"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 1_000_000}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- Business units ---------------------------------------------------------

class BusinessUnitCreate(_Strict):
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None


class BusinessUnitUpdate(_Strict):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    risk_owner_id: Optional[str] = None
    delegate_ids: Optional[List[str]] = None


class BusinessUnitOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    risk_owner_id: Optional[str] = None
    delegate_ids: List[str] = []


class RiskOwnerAssign(_Strict):
    person_id: Optional[str] = Field(None, description="null clears the risk owner")


# -- Assets -----------------------------------------------------------------

class AssetCreate(_Strict):
    name: str
    asset_type: str
    business_unit_id: str
    description: Optional[str] = None
    criticality: Optional[str] = None


class AssetUpdate(_Strict):
    name: Optional[str] = None
    asset_type: Optional[str] = None
    business_unit_id: Optional[str] = None
    description: Optional[str] = None
    criticality: Optional[str] = None


class AssetOut(AssetCreate):
    model_config = ConfigDict(extra="ignore")
    id: str


# -- Persons ----------------------------------------------------------------

class PersonCreate(_Strict):
    name: str
    title: Optional[str] = None
    email: Optional[str] = None


class PersonUpdate(_Strict):
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None


class PersonOut(PersonCreate):
    model_config = ConfigDict(extra="ignore")
    id: str


# -- Risks ------------------------------------------------------------------

class RiskCreate(_Strict):
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
    controls: List[str] = []


class RiskUpdate(_Strict):
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
    controls: Optional[List[str]] = None


class RiskOut(RiskCreate):
    model_config = ConfigDict(extra="ignore")
    id: str


# -- Simulation -------------------------------------------------------------

class SimulationRequest(_Strict):
    n_trials: int = Field(10_000, ge=100, le=1_000_000)
    seed: Optional[int] = None
    residual: bool = True
    unit_id: Optional[str] = Field(None, description="Only risks in this unit and its children")
