"""
Domain entities. Plain dataclasses with no knowledge of storage or presentation.

Relations are stored as IDs on the "child" side:
  BusinessUnit.parent_id, Asset.business_unit_id, Risk.business_unit_id, ...
so every backend can persist them the same way.
"""

from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


@dataclass
class Entity:
    kind: ClassVar[str] = "entity"
    id: str = field(default_factory=new_id, kw_only=True)

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
    specs describe the risk after controls.
    """
    kind: ClassVar[str] = "risk"
    name: str
    business_unit_id: str
    frequency: Dict[str, Any]
    impact: Dict[str, Any]
    description: Optional[str] = None
    category: Optional[str] = None  # e.g. "cyber", "operational", "financial"
    asset_ids: List[str] = field(default_factory=list)
    owner_id: Optional[str] = None
    residual_frequency: Optional[Dict[str, Any]] = None
    residual_impact: Optional[Dict[str, Any]] = None
    controls: List[str] = field(default_factory=list)


ENTITY_TYPES = (BusinessUnit, Asset, Person, Risk)
