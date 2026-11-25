"""
Domain entities. Plain dataclasses with no knowledge of storage or presentation.

Relations are stored as IDs on the referring side (see REFERENCES), so every
backend can persist them the same way and the service layer can check them.
"""

from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4

ORGANIZATION_TYPES = ("COMPANY", "DEPARTMENT", "SUBSIDIARY", "SUPPLIER")
CONTROL_TYPES = ("PREVENTIVE", "DETECTIVE", "CORRECTIVE", "DIRECTIVE")
IMPLEMENTATION_STATUSES = ("PLANNED", "IN_PROGRESS", "IMPLEMENTED", "RETIRED")
RISK_EVENT_TYPES = ("INCIDENT", "NEAR_MISS", "AUDIT_FINDING")
RISK_EVENT_STATUSES = ("OPEN", "INVESTIGATING", "CLOSED")
CONTROL_EVENT_TYPES = ("TEST", "REVIEW", "AUDIT")
CONTROL_EVENT_STATUSES = ("PASSED", "FAILED", "PARTIAL", "IN_PROGRESS")


def new_id() -> str:
    return str(uuid4())


@dataclass
class Entity:
    kind: ClassVar[str] = "entity"
    id: str = field(default_factory=new_id, kw_only=True)
    created_at: Optional[str] = field(default=None, kw_only=True)  # ISO timestamps, set by the service
    updated_at: Optional[str] = field(default=None, kw_only=True)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    @classmethod
    def field_names(cls) -> List[str]:
        return [f.name for f in fields(cls)]


@dataclass
class BusinessUnit(Entity):
    kind: ClassVar[str] = "business_unit"
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    code: Optional[str] = None
    organization_type: str = "DEPARTMENT"
    risk_owner_id: Optional[str] = None
    delegate_ids: List[str] = field(default_factory=list)


@dataclass
class Asset(Entity):
    kind: ClassVar[str] = "asset"
    name: str
    asset_type: str  # e.g. "physical", "virtual", "information"
    business_unit_id: str
    description: Optional[str] = None
    criticality: Optional[str] = None  # e.g. "low", "medium", "high", "critical"
    value: Optional[float] = None


@dataclass
class Person(Entity):
    kind: ClassVar[str] = "person"
    name: str
    title: Optional[str] = None
    email: Optional[str] = None


@dataclass
class Risk(Entity):
    """
    A quantified risk. `frequency` and `impact` are distribution specs
    (see risk_model.py) for the inherent risk; the optional residual_*
    specs describe the risk after controls. Qualitative 1-5 scores are
    derived from these specs, never stored.
    """
    kind: ClassVar[str] = "risk"
    name: str
    business_unit_id: str
    frequency: Dict[str, Any]
    impact: Dict[str, Any]
    description: Optional[str] = None
    category: Optional[str] = None  # risk type, e.g. "cyber", "operational"
    asset_ids: List[str] = field(default_factory=list)
    owner_id: Optional[str] = None
    residual_frequency: Optional[Dict[str, Any]] = None
    residual_impact: Optional[Dict[str, Any]] = None


@dataclass
class Control(Entity):
    kind: ClassVar[str] = "control"
    name: str
    business_unit_id: str
    description: Optional[str] = None
    control_type: str = "PREVENTIVE"
    implementation_status: str = "PLANNED"
    effectiveness_rating: Optional[int] = None  # 1 (weak) .. 5 (strong)
    owner_id: Optional[str] = None
    risk_ids: List[str] = field(default_factory=list)


@dataclass
class RiskEvent(Entity):
    """Something that happened: an incident, near miss or audit finding."""
    kind: ClassVar[str] = "risk_event"
    name: str
    risk_id: str
    date: str  # ISO date
    event_type: str = "INCIDENT"
    status: str = "OPEN"
    description: Optional[str] = None
    impact_description: Optional[str] = None
    financial_impact: Optional[float] = None


@dataclass
class ControlEvent(Entity):
    """A test, review or audit of a control."""
    kind: ClassVar[str] = "control_event"
    control_id: str
    date: str  # ISO date
    event_type: str = "TEST"
    status: str = "IN_PROGRESS"
    description: Optional[str] = None


@dataclass
class AuditEntry(Entity):
    """
    One link in the tamper-evident audit chain. Written by the service on every
    change; never updated or deleted, and not part of export/import.

    hash = SHA-256 over all other fields (canonical JSON), including prev_hash,
    so altering, removing or reordering an entry breaks the chain.
    state_hash = SHA-256 of the entity after the change (None after DELETE),
    so data edited outside RiskMesh can be detected.
    """
    kind: ClassVar[str] = "audit_entry"
    action: str  # CREATE, UPDATE, DELETE, IMPORT
    entity_type: str
    entity_id: Optional[str] = None
    details: Optional[str] = None
    user_id: str = "system"
    timestamp: str = ""
    seq: int = 0                                  # 1, 2, 3, ... without gaps
    source: str = "system"                        # api, cli, import:sha256:<...>, system
    reason: Optional[str] = None                  # why the change was made
    changes: Optional[Dict[str, Any]] = None      # {field: [before, after]}
    state_hash: Optional[str] = None
    prev_hash: str = ""
    hash: str = ""


@dataclass
class AnalysisRun(Entity):
    """
    A stored analysis result (the run register). `fingerprint` is the SHA-256
    of everything the result depends on: the risks and units in scope, the
    engine, its settings and version. While the data is unchanged the stored
    report is reused instead of recalculated; if the data changes the run
    becomes "stale" but stays in the register as a record of what was reported.
    """
    kind: ClassVar[str] = "analysis_run"
    number: int                      # 1, 2, 3, ... for referring to runs ("run #17")
    fingerprint: str
    mode: str                        # "single" or "compare" (inherent vs residual)
    engine: str                      # "analytic" or "monte_carlo"
    params: Dict[str, Any]           # n_trials, seed, residual, unit_id, engine version
    report: Dict[str, Any]
    created_by: str = "system"
    package_version: str = ""
    audit_seq: int = 0               # audit chain head when the run was made
    audit_hash: str = ""
    duration_ms: Optional[float] = None
    label: Optional[str] = None      # e.g. "Board meeting 2026-12-01"


# Data entities in dependency order (referenced types first)
DATA_TYPES = (Person, BusinessUnit, Asset, Risk, Control, RiskEvent, ControlEvent)
ENTITY_TYPES = DATA_TYPES + (AuditEntry, AnalysisRun)

# (from type, field, to type, field is a list, graph relationship name)
REFERENCES = [
    (BusinessUnit, "parent_id", BusinessUnit, False, "CHILD_OF"),
    (BusinessUnit, "risk_owner_id", Person, False, "HAS_RISK_OWNER"),
    (BusinessUnit, "delegate_ids", Person, True, "HAS_DELEGATE"),
    (Asset, "business_unit_id", BusinessUnit, False, "BELONGS_TO"),
    (Risk, "business_unit_id", BusinessUnit, False, "BELONGS_TO"),
    (Risk, "asset_ids", Asset, True, "AFFECTS"),
    (Risk, "owner_id", Person, False, "OWNED_BY"),
    (Control, "business_unit_id", BusinessUnit, False, "BELONGS_TO"),
    (Control, "owner_id", Person, False, "OWNED_BY"),
    (Control, "risk_ids", Risk, True, "MITIGATES"),
    (RiskEvent, "risk_id", Risk, False, "RELATED_TO"),
    (ControlEvent, "control_id", Control, False, "TESTS"),
]
