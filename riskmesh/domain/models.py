"""
Domain entities. Plain dataclasses with no knowledge of storage or presentation.

Relations are stored as IDs on the referring side (see REFERENCES), so every
backend can persist them the same way and the service layer can check them.
"""

from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4

ORGANIZATION_TYPES = ("COMPANY", "DEPARTMENT", "SUBSIDIARY", "SUPPLIER")
CONTROL_TYPES = ("PREVENTIVE", "DETECTIVE", "CORRECTIVE", "DIRECTIVE")
IMPLEMENTATION_STATUSES = ("PLANNED", "IN_PROGRESS", "IMPLEMENTED", "RETIRED")
RISK_EVENT_TYPES = ("INCIDENT", "NEAR_MISS", "AUDIT_FINDING")
RISK_EVENT_STATUSES = ("OPEN", "INVESTIGATING", "CLOSED")
CONTROL_EVENT_TYPES = ("TEST", "REVIEW", "AUDIT")
CONTROL_EVENT_STATUSES = ("PASSED", "FAILED", "PARTIAL", "IN_PROGRESS")
SERVICE_CRITICALITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
SUPPLIER_TIERS = ("CRITICAL", "IMPORTANT", "STANDARD")
# regimes a customer is subject to; they decide what the customer will ask of us
REGULATIONS = ("NIS2_ESSENTIAL", "NIS2_IMPORTANT", "DORA", "CER", "GDPR", "OTHER")
INCIDENT_STATUSES = ("OPEN", "CONTAINED", "RESOLVED", "CLOSED")
OBJECTIVE_CRITICALITIES = ("MISSION_CRITICAL", "IMPORTANT", "SUPPORTING")


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
        """
        Builds the entity from stored data. Unknown keys are ignored; missing
        or null values of fields with a default get that default. That is what
        makes adding a field to the model a safe, automatic migration for data
        written by an older version (JSON file, Neo4j node, new SQL column).
        """
        values = {}
        for f in fields(cls):
            if f.name in data and data[f.name] is not None:
                values[f.name] = data[f.name]
            elif f.name in data and f.default is MISSING and f.default_factory is MISSING:
                values[f.name] = None  # required field stored as null: let validation report it
        return cls(**values)

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
    # for a legal entity (the top company): what customers' DORA registers ask about us
    lei: Optional[str] = None                    # Legal Entity Identifier, 20 characters
    country: Optional[str] = None                # ISO country code, e.g. "DK"


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
    service_ids: List[str] = field(default_factory=list)    # services the risk can disrupt
    supplier_ids: List[str] = field(default_factory=list)   # suppliers the risk comes from
    # How long one event takes the linked services down (hours, same spec formats as impact).
    # With it, each event also costs the services' own hourly cost and the customers' penalties,
    # all driven by the same duration (common shock, see consequences.py).
    outage_hours: Optional[Dict[str, Any]] = None
    residual_outage_hours: Optional[Dict[str, Any]] = None
    # which categories of observed events and public benchmarks are evidence for this risk
    # (e.g. ["data breach", "phishing"]); the risk's own category always counts too
    evidence_categories: List[str] = field(default_factory=list)
    objective_ids: List[str] = field(default_factory=list)   # objectives this risk threatens directly


@dataclass
class Supplier(Entity):
    """
    A third party we depend on. `subcontractor_ids` are its own suppliers
    (our fourth parties), so a failure can be traced down the chain.
    """
    kind: ClassVar[str] = "supplier"
    name: str
    tier: str = "STANDARD"                       # CRITICAL | IMPORTANT | STANDARD
    description: Optional[str] = None
    country: Optional[str] = None
    owner_id: Optional[str] = None               # who manages the relationship
    contract_start: Optional[str] = None         # ISO dates
    contract_end: Optional[str] = None
    next_review: Optional[str] = None
    processes_personal_data: bool = False
    data_locations: List[str] = field(default_factory=list)   # e.g. ["DK", "EU"]
    certifications: List[str] = field(default_factory=list)   # e.g. ["ISO 27001", "ISAE 3402"]
    exit_plan: bool = False
    subcontractor_ids: List[str] = field(default_factory=list)
    lei: Optional[str] = None                    # Legal Entity Identifier (DORA register of information)
    company_number: Optional[str] = None         # national registration number, e.g. CVR
    parent_company: Optional[str] = None         # ultimate parent, if part of a group


@dataclass
class Service(Entity):
    """Something we deliver to customers, the systems it runs on and the suppliers it needs."""
    kind: ClassVar[str] = "service"
    name: str
    business_unit_id: str                        # who delivers it
    description: Optional[str] = None
    criticality: str = "MEDIUM"                  # CRITICAL | HIGH | MEDIUM | LOW
    owner_id: Optional[str] = None
    asset_ids: List[str] = field(default_factory=list)
    supplier_ids: List[str] = field(default_factory=list)
    rto_hours: Optional[float] = None            # recovery time objective
    rpo_hours: Optional[float] = None            # recovery point objective
    outage_cost_per_hour: Optional[float] = None  # our own cost while it is down (staff, recovery, lost sales)
    ict_service_type: Optional[str] = None       # e.g. "Cloud hosting (IaaS)", as customers' DORA registers ask
    data_locations: List[str] = field(default_factory=list)   # where its data is stored and processed


@dataclass
class Customer(Entity):
    """
    A customer, the services it uses and what its regulation and contract
    demand of us (DORA: whether we support a critical or important function).
    """
    kind: ClassVar[str] = "customer"
    name: str
    sector: Optional[str] = None                 # e.g. "municipal", "energy", "finance"
    regulations: List[str] = field(default_factory=list)
    service_ids: List[str] = field(default_factory=list)
    critical_function: bool = False
    account_owner_id: Optional[str] = None
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    contract_value: Optional[float] = None       # per year
    notification_hours: Optional[float] = None   # contractual deadline for telling them about an incident
    audit_rights: bool = False
    exit_support: bool = False
    notes: Optional[str] = None
    # Contractual penalty (or service credit) for an outage: per hour beyond the grace period, up to the cap
    penalty_per_hour: Optional[float] = None
    penalty_grace_hours: Optional[float] = None
    penalty_cap: Optional[float] = None          # per event


@dataclass
class Incident(Entity):
    """
    An incident as it happens: when we became aware of it, what it hits, and
    whom we have told. The deadlines (NIS2 reporting, each affected customer's
    contractual notification) are derived, never stored; `notifications`
    records when each was met: {"nis2:early_warning": "2026-11-27T14:05", ...}.
    Times are local, "YYYY-MM-DDTHH:MM".
    """
    kind: ClassVar[str] = "incident"
    title: str
    detected_at: str
    status: str = "OPEN"
    description: Optional[str] = None
    service_ids: List[str] = field(default_factory=list)     # services affected
    supplier_ids: List[str] = field(default_factory=list)    # suppliers where it started
    risk_id: Optional[str] = None                            # the risk that materialised
    owner_id: Optional[str] = None
    nis2_significant: Optional[bool] = None                  # None = not yet assessed
    resolved_at: Optional[str] = None
    root_cause: Optional[str] = None
    financial_impact: Optional[float] = None
    notifications: Dict[str, str] = field(default_factory=dict)


@dataclass
class Objective(Entity):
    """
    Something the organisation must achieve, e.g. "Keep the citizen portal
    available 99.9%" or "Deliver meter data to the energy market on time".
    Risks threaten it directly (Risk.objective_ids) or by disrupting the
    services that deliver it. The tolerance says how much loss against the
    objective is acceptable: at most `tolerance_probability` chance per year
    of losing more than `tolerance_loss`.
    """
    kind: ClassVar[str] = "objective"
    name: str
    criticality: str = "IMPORTANT"               # MISSION_CRITICAL | IMPORTANT | SUPPORTING
    description: Optional[str] = None
    business_unit_id: Optional[str] = None       # None: an objective of the whole organisation
    owner_id: Optional[str] = None
    service_ids: List[str] = field(default_factory=list)   # services that deliver it
    kpi: Optional[str] = None                    # how achievement is measured, e.g. "availability >= 99.9%"
    target_date: Optional[str] = None
    tolerance_loss: Optional[float] = None
    tolerance_probability: Optional[float] = None


@dataclass
class Observation(Entity):
    """
    Something that actually happened, as data: one event or a count of them
    (e.g. "12 phishing incidents in March"). Its level follows from what it
    names: a supplier, a business unit, or neither (the whole group).
    Used to check and calibrate the estimates of the risks it is evidence for.
    """
    kind: ClassVar[str] = "observation"
    date: str                                   # ISO date of the event (or the end of the period counted)
    category: str                               # e.g. "data breach", "security incident"
    count: int = 1
    loss: Optional[float] = None                # total loss of these events, if known
    business_unit_id: Optional[str] = None
    supplier_id: Optional[str] = None
    risk_id: Optional[str] = None               # when it is known which risk it was
    description: Optional[str] = None
    source: Optional[str] = None                # e.g. "incident log", "insurance claims 2025"


@dataclass
class Benchmark(Entity):
    """
    A public figure for organisations like ours, e.g. from a national
    statistics office, a data protection authority or an industry report:
    how often an event happens per year and/or what one costs. `weight` says
    how much it counts: in years of our own data for the frequency, in
    observed events for the loss.
    """
    kind: ClassVar[str] = "benchmark"
    name: str
    category: str
    source: Optional[str] = None                # citation or URL
    year: Optional[int] = None
    events_per_year: Optional[float] = None     # for an organisation like ours
    loss_p10: Optional[float] = None            # per event
    loss_p90: Optional[float] = None
    weight: float = 1.0
    notes: Optional[str] = None


@dataclass
class Driver(Entity):
    """
    A common cause behind several risks: "patch and access hygiene", "economic
    downturn", "hosting platform stability". Its `level` scales the risks it
    drives (1 = as estimated; 0.7 = 30% better, 1.5 = 50% worse), which is how
    interventions are tried out. `uncertainty` is how much it varies from year
    to year: the multiplier in a bad year (1 in 10), e.g. 1.5. A bad year of the
    driver is a bad year for every risk it drives, so the risks move together
    for a reason - applied by the Monte Carlo engine.
    """
    kind: ClassVar[str] = "driver"
    name: str
    description: Optional[str] = None
    owner_id: Optional[str] = None
    level: float = 1.0
    uncertainty: float = 1.0                     # P90 of the yearly multiplier (1 = no variation)
    indicator: Optional[str] = None              # what is measured, e.g. "% of critical patches within 14 days"


@dataclass
class DriverEffect(Entity):
    """
    How strongly a driver moves a risk: elasticities. With sensitivity 1 the
    frequency doubles when the driver doubles; 0.5 is a weaker link, 2 a
    stronger one, 0 none.
    """
    kind: ClassVar[str] = "driver_effect"
    driver_id: str
    risk_id: str
    frequency_sensitivity: float = 1.0
    impact_sensitivity: float = 0.0
    rationale: Optional[str] = None


@dataclass
class Scenario(Entity):
    """
    A named "what if the world turns": drivers at other levels ("economic climate
    1.5"), risks hit harder ({"<risk id>": {"frequency": 3, "impact": 1.5}}),
    and optionally an outage of a supplier or service for a number of hours.
    Evaluated against today on the total, the appetite and the objectives.
    """
    kind: ClassVar[str] = "scenario"
    name: str
    description: Optional[str] = None
    driver_levels: Dict[str, float] = field(default_factory=dict)          # driver id -> level
    risk_factors: Dict[str, Dict[str, float]] = field(default_factory=dict)  # risk id -> {"frequency", "impact"}
    outage_supplier_id: Optional[str] = None
    outage_service_id: Optional[str] = None
    outage_hours: Optional[float] = None
    likelihood: Optional[str] = None             # free text, e.g. "plausible within 5 years"


@dataclass
class Correlation(Entity):
    """
    How strongly two risks tend to have bad years together, from -1 to 1.
    Applied by the Monte Carlo engine as a rank (Gaussian copula) correlation
    between the risks' annual losses; each risk's own distribution is unchanged.
    """
    kind: ClassVar[str] = "correlation"
    risk_a_id: str
    risk_b_id: str
    coefficient: float
    rationale: Optional[str] = None


@dataclass
class Appetite(Entity):
    """
    Risk appetite as a tolerance curve: for each loss, the highest acceptable
    probability that the annual loss exceeds it. For the whole organisation,
    or one business unit including its children.
    """
    kind: ClassVar[str] = "appetite"
    name: str
    business_unit_id: Optional[str] = None   # None: the whole organisation
    points: List[Dict[str, float]] = field(default_factory=list)   # [{"loss": .., "probability": ..}]
    statement: Optional[str] = None
    owner_id: Optional[str] = None


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
    # what it costs and what it does, for decisions (see domain/decisions.py)
    implementation_cost: Optional[float] = None  # one-off
    annual_cost: Optional[float] = None          # running cost per year
    lifetime_years: float = 3.0                  # the implementation cost is spread over this
    frequency_reduction: Optional[float] = None  # share of events it prevents, 0-0.99
    impact_reduction: Optional[float] = None     # share of the loss per event it removes, 0-0.99
    driver_id: Optional[str] = None              # a driver it improves, e.g. patch automation -> patch hygiene
    driver_reduction: Optional[float] = None     # share it lowers that driver's level by, 0-0.9


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


ROLES = ("viewer", "editor", "admin")  # each role includes the rights of the ones before it


@dataclass
class User(Entity):
    """
    Someone who can log in. Local users have a password (scrypt hash);
    single-sign-on users (auth_source "oidc") are created on first login and
    identified by external_id = "<issuer>|<subject>".
    """
    kind: ClassVar[str] = "user"
    username: str
    role: str = "viewer"
    display_name: Optional[str] = None
    email: Optional[str] = None
    active: bool = True
    auth_source: str = "local"
    external_id: Optional[str] = None
    password_hash: Optional[str] = None


@dataclass
class ApiToken(Entity):
    """A login session or a long-lived API key. Only the SHA-256 of the token is stored."""
    kind: ClassVar[str] = "api_token"
    user_id: str
    token_hash: str
    token_type: str = "session"   # "session" (expires) or "api" (for scripts)
    name: Optional[str] = None
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    revoked: bool = False


@dataclass
class AuditAnchor(Entity):
    """
    A signed and/or third-party time-stamped statement "the audit chain had
    entry #seq with hash H at time T". Kept outside the chain so that removing
    the newest entries (which the chain itself cannot reveal) is detected.
    """
    kind: ClassVar[str] = "audit_anchor"
    seq: int
    head_hash: str
    statement: str                       # canonical JSON that was signed
    signature: Optional[str] = None      # Ed25519, base64
    public_key: Optional[str] = None     # PEM of the signing key
    key_id: Optional[str] = None         # short fingerprint of the public key
    tsa_url: Optional[str] = None        # RFC 3161 time-stamp authority
    tsa_token: Optional[str] = None      # DER time-stamp token, base64
    tsa_time: Optional[str] = None       # time asserted by the authority
    reason: Optional[str] = None         # e.g. "period 2026-Q4 closed"
    created_by: str = "system"


PERIOD_TYPES = ("month", "quarter", "year")


@dataclass
class ReportingPeriod(Entity):
    """A month, quarter or year whose figures are frozen when it is closed."""
    kind: ClassVar[str] = "reporting_period"
    key: str                             # "2026-11", "2026-Q4" or "2026"
    period_type: str
    start: str                           # ISO date, inclusive
    end: str                             # ISO date, inclusive
    status: str = "open"                 # open | closed
    closed_at: Optional[str] = None
    closed_by: Optional[str] = None
    run_id: Optional[str] = None         # the analysis run the figures come from
    anchor_id: Optional[str] = None      # audit anchor made at closing
    facts_hash: Optional[str] = None     # SHA-256 of the period's facts: verify_audit detects edits
    note: Optional[str] = None


@dataclass
class PeriodFact(Entity):
    """
    One row of the reporting fact table: the frozen figures of a closed period
    for the total, a business unit (incl. sub-units), a category or a risk.
    Flat on purpose, so Excel and Power BI can read it directly.
    """
    kind: ClassVar[str] = "period_fact"
    period_key: str
    period_end: str
    level: str                           # total | unit | category | risk
    entity_id: Optional[str] = None
    name: Optional[str] = None
    business_unit_id: Optional[str] = None
    category: Optional[str] = None
    risk_count: Optional[int] = None
    high_risk_count: Optional[int] = None
    eal_inherent: Optional[float] = None
    eal_residual: Optional[float] = None
    p50: Optional[float] = None
    p90: Optional[float] = None
    p99: Optional[float] = None
    tvar99: Optional[float] = None
    p99_inherent: Optional[float] = None
    likelihood_inherent: Optional[int] = None
    impact_inherent: Optional[int] = None
    score_inherent: Optional[float] = None
    likelihood_residual: Optional[int] = None
    impact_residual: Optional[int] = None
    score_residual: Optional[float] = None
    controls: Optional[int] = None
    control_effectiveness: Optional[float] = None
    events: Optional[int] = None
    actual_loss: Optional[float] = None


# Data entities in dependency order (referenced types first)
DATA_TYPES = (Person, BusinessUnit, Asset, Supplier, Service, Customer, Objective, Risk, Driver, DriverEffect,
              Scenario, Correlation, Appetite, Control,
              RiskEvent, ControlEvent, Incident, Observation, Benchmark)
ENTITY_TYPES = DATA_TYPES + (AuditEntry, AnalysisRun, User, ApiToken, AuditAnchor, ReportingPeriod, PeriodFact)
# entities whose stored state verify_audit() compares with the audit trail
VERIFIED_TYPES = DATA_TYPES + (User, ReportingPeriod)

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
    (Risk, "service_ids", Service, True, "DISRUPTS"),
    (Risk, "supplier_ids", Supplier, True, "ORIGINATES_AT"),
    (Supplier, "owner_id", Person, False, "OWNED_BY"),
    (Supplier, "subcontractor_ids", Supplier, True, "SUBCONTRACTS_TO"),
    (Service, "business_unit_id", BusinessUnit, False, "DELIVERED_BY"),
    (Service, "owner_id", Person, False, "OWNED_BY"),
    (Service, "asset_ids", Asset, True, "RUNS_ON"),
    (Service, "supplier_ids", Supplier, True, "DEPENDS_ON"),
    (Customer, "service_ids", Service, True, "USES"),
    (Customer, "account_owner_id", Person, False, "ACCOUNT_OWNER"),
    (Driver, "owner_id", Person, False, "OWNED_BY"),
    (DriverEffect, "driver_id", Driver, False, "DRIVES"),
    (DriverEffect, "risk_id", Risk, False, "DRIVEN"),
    (Scenario, "outage_supplier_id", Supplier, False, "SCENARIO_OUTAGE"),
    (Scenario, "outage_service_id", Service, False, "SCENARIO_OUTAGE"),
    (Control, "driver_id", Driver, False, "IMPROVES"),
    (Correlation, "risk_a_id", Risk, False, "CORRELATES"),
    (Correlation, "risk_b_id", Risk, False, "CORRELATES"),
    (Appetite, "business_unit_id", BusinessUnit, False, "APPLIES_TO"),
    (Appetite, "owner_id", Person, False, "OWNED_BY"),
    (Incident, "service_ids", Service, True, "AFFECTED"),
    (Incident, "supplier_ids", Supplier, True, "STARTED_AT"),
    (Incident, "risk_id", Risk, False, "MATERIALISED"),
    (Incident, "owner_id", Person, False, "HANDLED_BY"),
    (Objective, "business_unit_id", BusinessUnit, False, "PURSUED_BY"),
    (Objective, "owner_id", Person, False, "OWNED_BY"),
    (Objective, "service_ids", Service, True, "DELIVERED_THROUGH"),
    (Risk, "objective_ids", Objective, True, "THREATENS"),
    (Observation, "business_unit_id", BusinessUnit, False, "OBSERVED_AT"),
    (Observation, "supplier_id", Supplier, False, "OBSERVED_AT"),
    (Observation, "risk_id", Risk, False, "EVIDENCE_FOR"),
]
