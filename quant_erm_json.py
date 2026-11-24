
import json
import os
from uuid import uuid4
from datetime import datetime
import logging
from typing import Optional, List, Dict, Any

import risk_model
from risk_model import summarize_distribution  # noqa: F401  (re-exported)

# =========================================================================
# Configuration
# =========================================================================

JSON_FILE = "risk_management_data.json"
LOG_FILE = "risk_management.log"

# =========================================================================
# Logging Setup
# =========================================================================

logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def log_message(message, level=logging.INFO):
    """
    Logs messages with a timestamp.  Uses the configured logging.
    """
    if level == logging.INFO:
        logging.info(message)
    elif level == logging.WARNING:
        logging.warning(message)
    elif level == logging.ERROR:
        logging.error(message)
    elif level == logging.DEBUG:
        logging.debug(message)
    else:
        logging.info(message)  # Default to INFO for unknown levels

# =========================================================================
# JSON Storage
# =========================================================================

class JsonStorage:
    """
    Manages JSON file storage and operations.
    """
    def __init__(self, filename: str):
        self.filename = filename
        self._data = self._load_data()
    
    def _load_data(self) -> Dict[str, Any]:
        """
        Loads data from JSON file or creates a new structure.
        """
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                for key in ("business_units", "assets", "persons", "risks"):
                    data.setdefault(key, {})
                return data
            except json.JSONDecodeError:
                log_message("Invalid JSON file, creating new data structure", logging.WARNING)
                return self._create_empty_structure()
        else:
            log_message(f"JSON file {self.filename} not found, creating new one", logging.INFO)
            return self._create_empty_structure()
    
    def _create_empty_structure(self) -> Dict[str, Any]:
        """
        Creates an empty data structure.
        """
        return {
            "business_units": {},
            "assets": {},
            "persons": {},
            "risks": {},
            "version": "1.1",
            "last_modified": datetime.now().isoformat()
        }
    
    def save(self):
        """
        Saves data to JSON file.
        """
        self._data["last_modified"] = datetime.now().isoformat()
        with open(self.filename, 'w') as f:
            json.dump(self._data, f, indent=4)
        log_message(f"Data saved to {self.filename}")
    
    def get_business_unit(self, unit_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a business unit by ID.
        """
        return self._data["business_units"].get(unit_id)
    
    def add_business_unit(self, unit_id: str, data: Dict[str, Any]):
        """
        Adds a business unit to storage.
        """
        self._data["business_units"][unit_id] = data
        self.save()
    
    def update_business_unit(self, unit_id: str, data: Dict[str, Any]):
        """
        Updates a business unit in storage.
        """
        if unit_id in self._data["business_units"]:
            self._data["business_units"][unit_id].update(data)
            self.save()
    
    def delete_business_unit(self, unit_id: str):
        """
        Deletes a business unit from storage.
        """
        if unit_id in self._data["business_units"]:
            del self._data["business_units"][unit_id]
            self.save()
    
    def get_asset(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves an asset by ID.
        """
        return self._data["assets"].get(asset_id)
    
    def add_asset(self, asset_id: str, data: Dict[str, Any]):
        """
        Adds an asset to storage.
        """
        self._data["assets"][asset_id] = data
        self.save()
    
    def update_asset(self, asset_id: str, data: Dict[str, Any]):
        """
        Updates an asset in storage.
        """
        if asset_id in self._data["assets"]:
            self._data["assets"][asset_id].update(data)
            self.save()
    
    def delete_asset(self, asset_id: str):
        """
        Deletes an asset from storage.
        """
        if asset_id in self._data["assets"]:
            del self._data["assets"][asset_id]
            self.save()
    
    def get_person(self, person_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a person by ID.
        """
        return self._data["persons"].get(person_id)
    
    def add_person(self, person_id: str, data: Dict[str, Any]):
        """
        Adds a person to storage.
        """
        self._data["persons"][person_id] = data
        self.save()
    
    def update_person(self, person_id: str, data: Dict[str, Any]):
        """
        Updates a person in storage.
        """
        if person_id in self._data["persons"]:
            self._data["persons"][person_id].update(data)
            self.save()
    
    def delete_person(self, person_id: str):
        """
        Deletes a person from storage.
        """
        if person_id in self._data["persons"]:
            del self._data["persons"][person_id]
            self.save()
    
    def get_risk(self, risk_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a risk by ID.
        """
        return self._data["risks"].get(risk_id)

    def add_risk(self, risk_id: str, data: Dict[str, Any]):
        """
        Adds a risk to storage.
        """
        self._data["risks"][risk_id] = data
        self.save()

    def update_risk(self, risk_id: str, data: Dict[str, Any]):
        """
        Updates a risk in storage.
        """
        if risk_id in self._data["risks"]:
            self._data["risks"][risk_id].update(data)
            self.save()

    def delete_risk(self, risk_id: str):
        """
        Deletes a risk from storage.
        """
        if risk_id in self._data["risks"]:
            del self._data["risks"][risk_id]
            self.save()

    def get_all_risks(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns all risks.
        """
        return self._data["risks"]

    def get_all_business_units(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns all business units.
        """
        return self._data["business_units"]
    
    def get_all_assets(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns all assets.
        """
        return self._data["assets"]
    
    def get_all_persons(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns all persons.
        """
        return self._data["persons"]
    
    def clear_all_data(self):
        """
        Clears all data and creates a new empty structure.
        """
        self._data = self._create_empty_structure()
        self.save()

# =========================================================================
# Data Model Classes
# =========================================================================

class BusinessUnit:
    """
    Represents a business unit in the organization.
    """
    def __init__(self, name, description=None, parent_id=None):
        """
        Initializes a BusinessUnit object.
        """
        self.id = str(uuid4())
        self.name = name
        self.description = description
        self.parent_id = parent_id  # ID of the parent BusinessUnit
        self.asset_ids = []  # List of Asset IDs
        self.risk_owner_id = None  # Person ID
        self.delegate_ids = []  # List of Person IDs

    def __repr__(self):
        return f"BusinessUnit(id='{self.id}', name='{self.name}')"

    def to_dict(self):
        """
        Returns a dictionary representation of the BusinessUnit.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "parent_id": self.parent_id,
            "asset_ids": self.asset_ids,
            "risk_owner_id": self.risk_owner_id,
            "delegate_ids": self.delegate_ids,
        }

class Asset:
    """
    Represents an asset (physical, virtual, or informational).
    """
    def __init__(self, name, asset_type, description=None, criticality=None):
        """
        Initializes an Asset object.
        """
        self.id = str(uuid4())
        self.name = name
        self.asset_type = asset_type  # e.g., "server", "database", "document", "physical"
        self.description = description
        self.criticality = criticality  # e.g., "high", "medium", "low"
        self.business_unit_id = None  # ID of the owning business unit

    def __repr__(self):
        return f"Asset(id='{self.id}', name='{self.name}', type='{self.asset_type}')"

    def to_dict(self):
        """
        Returns a dictionary representation of the Asset.
        """
        return {
            "id": self.id,
            "name": self.name,
            "asset_type": self.asset_type,
            "description": self.description,
            "criticality": self.criticality,
            "business_unit_id": self.business_unit_id,
        }

class Person:
    """
    Represents a person in the organization.
    """
    def __init__(self, name, title=None, email=None):
        """
        Initializes a Person object.
        """
        self.id = str(uuid4())
        self.name = name
        self.title = title
        self.email = email

    def __repr__(self):
        return f"Person(id='{self.id}', name='{self.name}')"

    def to_dict(self):
        """
        Returns a dictionary representation of the Person.
        """
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "email": self.email,
        }

class Risk:
    """
    Represents a quantified risk owned by a business unit.

    `frequency` and `impact` are distribution specs (see risk_model.py)
    describing the inherent risk. The optional `residual_frequency` and
    `residual_impact` describe the risk after controls.
    """
    def __init__(self, name, business_unit_id, frequency, impact,
                 description=None, category=None, asset_ids=None, owner_id=None,
                 residual_frequency=None, residual_impact=None, controls=None):
        self.id = str(uuid4())
        self.name = name
        self.description = description
        self.category = category  # e.g. "operational", "cyber", "financial"
        self.business_unit_id = business_unit_id
        self.asset_ids = list(asset_ids or [])
        self.owner_id = owner_id
        self.frequency = frequency
        self.impact = impact
        self.residual_frequency = residual_frequency
        self.residual_impact = residual_impact
        self.controls = list(controls or [])  # free-text descriptions

    def __repr__(self):
        return f"Risk(id='{self.id}', name='{self.name}')"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "business_unit_id": self.business_unit_id,
            "asset_ids": self.asset_ids,
            "owner_id": self.owner_id,
            "frequency": self.frequency,
            "impact": self.impact,
            "residual_frequency": self.residual_frequency,
            "residual_impact": self.residual_impact,
            "controls": self.controls,
        }

    @classmethod
    def from_dict(cls, data):
        risk = cls(
            name=data["name"],
            business_unit_id=data["business_unit_id"],
            frequency=data["frequency"],
            impact=data["impact"],
            description=data.get("description"),
            category=data.get("category"),
            asset_ids=data.get("asset_ids"),
            owner_id=data.get("owner_id"),
            residual_frequency=data.get("residual_frequency"),
            residual_impact=data.get("residual_impact"),
            controls=data.get("controls"),
        )
        risk.id = data["id"]
        return risk

# =========================================================================
# Business Layer Functions
# =========================================================================

class RiskManager:
    """
    Handles the business logic for managing the organization structure and risk data.
    """
    def __init__(self, storage: JsonStorage):
        """
        Initializes the RiskManager with a JSON storage backend.
        """
        self.storage = storage

    def create_business_unit(self, name, description=None, parent_id=None):
        """
        Creates a new business unit.
        """
        if not name:
            raise ValueError("Business unit name cannot be empty.")
        
        # Check for duplicate name
        for bu_id, bu_data in self.storage.get_all_business_units().items():
            if bu_data["name"] == name:
                raise ValueError(f"Business unit with name '{name}' already exists.")

        bu = BusinessUnit(name, description, parent_id)
        self.storage.add_business_unit(bu.id, bu.to_dict())
        log_message(f"Created business unit: {bu}")
        return bu

    def get_business_unit(self, unit_id):
        """
        Retrieves a business unit by its ID.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        
        bu_data = self.storage.get_business_unit(unit_id)
        if not bu_data:
            return None
        
        bu = BusinessUnit(
            name=bu_data['name'],
            description=bu_data['description'],
            parent_id=bu_data['parent_id']
        )
        bu.id = bu_data['id']
        bu.asset_ids = bu_data.get('asset_ids', [])
        bu.risk_owner_id = bu_data.get('risk_owner_id')
        bu.delegate_ids = bu_data.get('delegate_ids', [])
        
        return bu

    def update_business_unit(self, unit_id, name=None, description=None, parent_id=None):
        """
        Updates an existing business unit.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")

        # Fetch the existing business unit to ensure it exists
        existing_bu = self.get_business_unit(unit_id)
        if not existing_bu:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")

        # Update only the provided fields
        update_data = {}
        if name is not None:
            existing_bu.name = name
            update_data['name'] = name
        if description is not None:
            existing_bu.description = description
            update_data['description'] = description
        if parent_id is not None:
            existing_bu.parent_id = parent_id
            update_data['parent_id'] = parent_id

        self.storage.update_business_unit(unit_id, update_data)
        log_message(f"Updated business unit: {existing_bu}")
        return existing_bu

    def delete_business_unit(self, unit_id):
        """
        Deletes a business unit by its ID.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        
        # Check if the business unit exists
        existing_bu = self.get_business_unit(unit_id)
        if not existing_bu:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")

        # Delete associated assets
        for asset_id in existing_bu.asset_ids:
            self.storage.delete_asset(asset_id)

        # Delete risks owned by the business unit
        for risk_id, risk_data in list(self.storage.get_all_risks().items()):
            if risk_data.get("business_unit_id") == unit_id:
                self.storage.delete_risk(risk_id)
        
        self.storage.delete_business_unit(unit_id)
        log_message(f"Deleted business unit with ID: {unit_id}")

    def create_asset(self, unit_id, name, asset_type, description=None, criticality=None):
        """
        Creates a new asset and assigns it to a business unit.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        if not name:
            raise ValueError("Asset name cannot be empty.")
        if not asset_type:
            raise ValueError("Asset type cannot be empty.")

        # Check if the business unit exists
        business_unit = self.get_business_unit(unit_id)
        if not business_unit:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")

        asset = Asset(name, asset_type, description, criticality)
        asset.business_unit_id = unit_id
        
        # Add asset to storage
        self.storage.add_asset(asset.id, asset.to_dict())
        
        # Update business unit's asset list
        business_unit.asset_ids.append(asset.id)
        self.storage.update_business_unit(unit_id, {"asset_ids": business_unit.asset_ids})
        
        log_message(f"Created asset: {asset} for business unit: {unit_id}")
        return asset

    def get_asset(self, asset_id):
        """
        Retrieves an asset by its ID.
        """
        if not asset_id:
            raise ValueError("Asset ID cannot be empty.")
        
        asset_data = self.storage.get_asset(asset_id)
        if not asset_data:
            return None
        
        asset = Asset(
            name=asset_data['name'],
            asset_type=asset_data['asset_type'],
            description=asset_data['description'],
            criticality=asset_data['criticality']
        )
        asset.id = asset_data['id']
        asset.business_unit_id = asset_data.get('business_unit_id')
        
        return asset

    def update_asset(self, asset_id, name=None, asset_type=None, description=None, criticality=None):
        """
        Updates an existing asset.
        """
        if not asset_id:
            raise ValueError("Asset ID cannot be empty.")
        
        existing_asset = self.get_asset(asset_id)
        if not existing_asset:
            raise ValueError(f"Asset with ID '{asset_id}' not found.")

        # Update only the provided fields
        update_data = {}
        if name is not None:
            existing_asset.name = name
            update_data['name'] = name
        if asset_type is not None:
            existing_asset.asset_type = asset_type
            update_data['asset_type'] = asset_type
        if description is not None:
            existing_asset.description = description
            update_data['description'] = description
        if criticality is not None:
            existing_asset.criticality = criticality
            update_data['criticality'] = criticality

        self.storage.update_asset(asset_id, update_data)
        log_message(f"Updated asset: {existing_asset}")
        return existing_asset

    def delete_asset(self, asset_id):
        """
        Deletes an asset by its ID.
        """
        if not asset_id:
            raise ValueError("Asset ID cannot be empty.")
        
        # Check if the asset exists
        existing_asset = self.get_asset(asset_id)
        if not existing_asset:
            raise ValueError(f"Asset with ID '{asset_id}' not found.")
        
        # Remove asset from business unit's asset list
        if existing_asset.business_unit_id:
            bu = self.get_business_unit(existing_asset.business_unit_id)
            if bu and asset_id in bu.asset_ids:
                bu.asset_ids.remove(asset_id)
                self.storage.update_business_unit(bu.id, {"asset_ids": bu.asset_ids})
        
        # Unlink the asset from any risks
        for risk_id, risk_data in self.storage.get_all_risks().items():
            if asset_id in risk_data.get("asset_ids", []):
                remaining = [a for a in risk_data["asset_ids"] if a != asset_id]
                self.storage.update_risk(risk_id, {"asset_ids": remaining})

        self.storage.delete_asset(asset_id)
        log_message(f"Deleted asset with ID: {asset_id}")

    def create_person(self, name, title=None, email=None):
        """
        Creates a new person.
        """
        if not name:
            raise ValueError("Person name cannot be empty.")
        
        person = Person(name, title, email)
        self.storage.add_person(person.id, person.to_dict())
        log_message(f"Created person: {person}")
        return person

    def get_person(self, person_id):
        """
        Retrieves a person by their ID.
        """
        if not person_id:
            raise ValueError("Person ID cannot be empty.")
        
        person_data = self.storage.get_person(person_id)
        if not person_data:
            return None
        
        person = Person(
            name=person_data['name'],
            title=person_data['title'],
            email=person_data['email']
        )
        person.id = person_data['id']
        
        return person

    def update_person(self, person_id, name=None, title=None, email=None):
        """
        Updates an existing person.
        """
        if not person_id:
            raise ValueError("Person ID cannot be empty.")
        
        existing_person = self.get_person(person_id)
        if not existing_person:
            raise ValueError(f"Person with ID '{person_id}' not found.")

        # Update only the provided fields
        update_data = {}
        if name is not None:
            existing_person.name = name
            update_data['name'] = name
        if title is not None:
            existing_person.title = title
            update_data['title'] = title
        if email is not None:
            existing_person.email = email
            update_data['email'] = email

        self.storage.update_person(person_id, update_data)
        log_message(f"Updated person: {existing_person}")
        return existing_person

    def delete_person(self, person_id):
        """
        Deletes a person by their ID.
        """
        if not person_id:
            raise ValueError("Person ID cannot be empty.")
        
        # Check if the person exists
        existing_person = self.get_person(person_id)
        if not existing_person:
            raise ValueError(f"Person with ID '{person_id}' not found.")
        
        self.storage.delete_person(person_id)
        log_message(f"Deleted person with ID: {person_id}")

    def assign_risk_owner(self, unit_id, person_id):
        """
        Assigns a person as the risk owner for a business unit.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        if not person_id:
            raise ValueError("Person ID cannot be empty.")

        # Check if the business unit and person exist
        business_unit = self.get_business_unit(unit_id)
        if not business_unit:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")
        person = self.get_person(person_id)
        if not person:
            raise ValueError(f"Person with ID '{person_id}' not found.")
        
        business_unit.risk_owner_id = person_id
        self.storage.update_business_unit(unit_id, {"risk_owner_id": person_id})
        log_message(f"Assigned {person} as risk owner for {business_unit}")

    def assign_delegate(self, unit_id, person_id):
        """
        Assigns a person as a risk management delegate for a business unit.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        if not person_id:
            raise ValueError("Person ID cannot be empty.")
        
        # Check if the business unit and person exist
        business_unit = self.get_business_unit(unit_id)
        if not business_unit:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")
        person = self.get_person(person_id)
        if not person:
            raise ValueError(f"Person with ID '{person_id}' not found.")
        
        if person_id not in business_unit.delegate_ids:
            business_unit.delegate_ids.append(person_id)
            self.storage.update_business_unit(unit_id, {"delegate_ids": business_unit.delegate_ids})
            log_message(f"Assigned {person} as delegate for {business_unit}")

    def remove_delegate(self, unit_id, person_id):
        """
        Removes a person as a risk management delegate from a business unit.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        if not person_id:
            raise ValueError("Person ID cannot be empty.")

        # Check if the business unit and person exist
        business_unit = self.get_business_unit(unit_id)
        if not business_unit:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")
        person = self.get_person(person_id)
        if not person:
            raise ValueError(f"Person with ID '{person_id}' not found.")

        if person_id in business_unit.delegate_ids:
            business_unit.delegate_ids.remove(person_id)
            self.storage.update_business_unit(unit_id, {"delegate_ids": business_unit.delegate_ids})
            log_message(f"Removed delegate {person.name} from business unit {business_unit.name}")

    # ---------------------------------------------------------------------
    # Risks
    # ---------------------------------------------------------------------

    def _validate_risk_specs(self, frequency, impact):
        risk_model.validate_frequency(frequency)
        risk_model.build_impact_distribution(impact)

    def create_risk(self, unit_id, name, frequency, impact, description=None,
                    category=None, asset_ids=None, owner_id=None,
                    residual_frequency=None, residual_impact=None, controls=None):
        """
        Creates a quantified risk for a business unit.
        """
        if not name:
            raise ValueError("Risk name cannot be empty.")
        if not self.get_business_unit(unit_id):
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")
        for asset_id in asset_ids or []:
            if not self.get_asset(asset_id):
                raise ValueError(f"Asset with ID '{asset_id}' not found.")
        if owner_id and not self.get_person(owner_id):
            raise ValueError(f"Person with ID '{owner_id}' not found.")
        self._validate_risk_specs(frequency, impact)
        if residual_frequency or residual_impact:
            self._validate_risk_specs(residual_frequency or frequency, residual_impact or impact)

        risk = Risk(name, unit_id, frequency, impact, description, category,
                    asset_ids, owner_id, residual_frequency, residual_impact, controls)
        self.storage.add_risk(risk.id, risk.to_dict())
        log_message(f"Created risk: {risk} for business unit: {unit_id}")
        return risk

    def get_risk(self, risk_id):
        """
        Retrieves a risk by its ID.
        """
        if not risk_id:
            raise ValueError("Risk ID cannot be empty.")
        data = self.storage.get_risk(risk_id)
        return Risk.from_dict(data) if data else None

    def list_risks(self, unit_id=None):
        """
        Returns all risks, optionally only those of one business unit.
        """
        return [Risk.from_dict(d) for d in self.storage.get_all_risks().values()
                if unit_id is None or d.get("business_unit_id") == unit_id]

    def update_risk(self, risk_id, **changes):
        """
        Updates fields of a risk (e.g. impact={...}, residual_frequency={...}).
        """
        risk = self.get_risk(risk_id)
        if not risk:
            raise ValueError(f"Risk with ID '{risk_id}' not found.")
        allowed = set(risk.to_dict()) - {"id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unknown risk fields: {', '.join(sorted(unknown))}")
        for key, value in changes.items():
            setattr(risk, key, value)
        if "business_unit_id" in changes and not self.get_business_unit(risk.business_unit_id):
            raise ValueError(f"Business unit with ID '{risk.business_unit_id}' not found.")
        self._validate_risk_specs(risk.frequency, risk.impact)
        if risk.residual_frequency or risk.residual_impact:
            self._validate_risk_specs(risk.residual_frequency or risk.frequency,
                                      risk.residual_impact or risk.impact)
        self.storage.update_risk(risk_id, changes)
        log_message(f"Updated risk: {risk}")
        return risk

    def delete_risk(self, risk_id):
        """
        Deletes a risk by its ID.
        """
        if not self.get_risk(risk_id):
            raise ValueError(f"Risk with ID '{risk_id}' not found.")
        self.storage.delete_risk(risk_id)
        log_message(f"Deleted risk with ID: {risk_id}")

    def simulate(self, n_trials=10_000, seed=None, residual=True):
        """
        Runs a Monte Carlo simulation of all risks and returns a
        risk_model.SimulationResult (use .report() for key figures).
        """
        risks = self.storage.get_all_risks()
        if not risks:
            raise ValueError("There are no risks to simulate.")
        result = risk_model.run_simulation(risks, self.storage.get_all_business_units(),
                                           n_trials=n_trials, seed=seed, residual=residual)
        log_message(f"Simulated {len(risks)} risks over {n_trials} trials "
                    f"({'residual' if residual else 'inherent'})")
        return result

    def save_simulation_report(self, report, filename=None):
        """
        Writes a simulation report to a JSON file and returns the filename.
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"risk_simulation_{timestamp}.json"
        with open(filename, "w") as f:
            json.dump(report, f, indent=4)
        log_message(f"Saved simulation report to {filename}")
        return filename

    def export_data(self):
        """
        Exports all data to a timestamped JSON file.
        """
        try:
            # Collect all data
            business_units = []
            for bu_id, bu_data in self.storage.get_all_business_units().items():
                bu = self.get_business_unit(bu_id)
                if bu:
                    # Get assets for this business unit
                    assets = []
                    for asset_id in bu.asset_ids:
                        asset = self.get_asset(asset_id)
                        if asset:
                            assets.append(asset.to_dict())
                    
                    bu_dict = bu.to_dict()
                    bu_dict['assets'] = assets
                    business_units.append(bu_dict)
            
            # Get all persons
            persons = []
            for person_id, person_data in self.storage.get_all_persons().items():
                person = self.get_person(person_id)
                if person:
                    persons.append(person.to_dict())
            
            # Get all assets
            all_assets = []
            for asset_id, asset_data in self.storage.get_all_assets().items():
                asset = self.get_asset(asset_id)
                if asset:
                    all_assets.append(asset.to_dict())
            
            export_data = {
                "business_units": business_units,
                "persons": persons,
                "assets": all_assets,
                "risks": [r.to_dict() for r in self.list_risks()],
            }
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"risk_data_export_{timestamp}.json"
            with open(filename, "w") as f:
                json.dump(export_data, f, indent=4)
            log_message(f"Exported data to {filename}")
            return filename
        except Exception as e:
            log_message(f"Error exporting data: {e}", logging.ERROR)
            raise

    def import_data(self, filename):
        """
        Imports data from a JSON file.
        """
        try:
            with open(filename, "r") as f:
                import_data = json.load(f)

            # Clear existing data
            self.storage.clear_all_data()
            log_message("Cleared existing data.")

            # Import Persons first
            persons = import_data.get("persons", [])
            person_map = {}  # Maps old IDs to new IDs
            for person_data in persons:
                old_id = person_data['id']
                person = self.create_person(
                    name=person_data['name'],
                    title=person_data.get('title'),
                    email=person_data.get('email')
                )
                person_map[old_id] = person.id

            # Import Assets next
            assets = import_data.get("assets", [])
            asset_map = {}  # Maps old IDs to new IDs
            for asset_data in assets:
                old_id = asset_data['id']
                asset = Asset(
                    name=asset_data['name'],
                    asset_type=asset_data['asset_type'],
                    description=asset_data.get('description'),
                    criticality=asset_data.get('criticality')
                )
                asset.id = str(uuid4())  # Generate new ID
                asset.business_unit_id = asset_data.get('business_unit_id')
                self.storage.add_asset(asset.id, asset.to_dict())
                asset_map[old_id] = asset.id

            # Import Business Units
            business_units = import_data.get("business_units", [])
            bu_map = {}  # Maps old IDs to new IDs
            
            # First pass: create all business units
            for bu_data in business_units:
                old_id = bu_data['id']
                bu = self.create_business_unit(
                    name=bu_data['name'],
                    description=bu_data.get('description'),
                    parent_id=None  # Set parent IDs in second pass
                )
                bu_map[old_id] = bu.id
            
            # Second pass: update parent IDs and other relationships
            for bu_data in business_units:
                old_id = bu_data['id']
                new_id = bu_map[old_id]
                
                # Update parent ID
                if bu_data.get('parent_id') and bu_data['parent_id'] in bu_map:
                    self.storage.update_business_unit(new_id, {"parent_id": bu_map[bu_data['parent_id']]})
                
                # Update asset IDs
                new_asset_ids = []
                for asset_data in bu_data.get('assets', []):
                    old_asset_id = asset_data['id']
                    if old_asset_id in asset_map:
                        new_asset_id = asset_map[old_asset_id]
                        new_asset_ids.append(new_asset_id)
                        # Update asset's business unit ID
                        self.storage.update_asset(new_asset_id, {"business_unit_id": new_id})
                
                self.storage.update_business_unit(new_id, {"asset_ids": new_asset_ids})
                
                # Assign risk owner
                if bu_data.get('risk_owner_id') and bu_data['risk_owner_id'] in person_map:
                    self.assign_risk_owner(new_id, person_map[bu_data['risk_owner_id']])
                
                # Assign delegates
                for old_delegate_id in bu_data.get('delegate_ids', []):
                    if old_delegate_id in person_map:
                        self.assign_delegate(new_id, person_map[old_delegate_id])
            
            # Import Risks, remapping references to the new IDs
            for risk_data in import_data.get("risks", []):
                unit_id = bu_map.get(risk_data.get("business_unit_id"))
                if not unit_id:
                    log_message(f"Skipping risk '{risk_data.get('name')}': unknown business unit",
                                logging.WARNING)
                    continue
                self.create_risk(
                    unit_id=unit_id,
                    name=risk_data["name"],
                    frequency=risk_data["frequency"],
                    impact=risk_data["impact"],
                    description=risk_data.get("description"),
                    category=risk_data.get("category"),
                    asset_ids=[asset_map[a] for a in risk_data.get("asset_ids", []) if a in asset_map],
                    owner_id=person_map.get(risk_data.get("owner_id")),
                    residual_frequency=risk_data.get("residual_frequency"),
                    residual_impact=risk_data.get("residual_impact"),
                    controls=risk_data.get("controls"),
                )

            log_message(f"Imported data from {filename}")
        except Exception as e:
            log_message(f"Error importing data: {e}", logging.ERROR)
            raise

    def get_organisation_structure(self):
        """
        Retrieves the organisation structure.
        
        Returns:
            list: A list of dictionaries representing the organisation structure.
        """
        try:
            organisation_structure = []
            for bu_id, bu_data in self.storage.get_all_business_units().items():
                organisation_structure.append({
                    "id": bu_data["id"],
                    "name": bu_data["name"],
                    "parent_id": bu_data.get("parent_id")
                })
            return organisation_structure
        except Exception as e:
            log_message(f"Error retrieving organisation structure: {e}", logging.ERROR)
            raise

# =========================================================================
# Reporting
# =========================================================================

def format_money(value):
    return f"{value:>14,.0f}"


def print_report(report, title):
    """
    Prints the key figures of a simulation report.
    """
    print(f"\n=== {title} ({report['n_trials']:,} simulated years) ===")
    t = report["total"]
    print(f"Expected annual loss : {format_money(t['mean'])}")
    print(f"Median (P50)         : {format_money(t['p50'])}")
    print(f"P90                  : {format_money(t['p90'])}")
    print(f"VaR 99 (P99)         : {format_money(t['p99'])}")
    print(f"TVaR 99              : {format_money(t['tvar99'])}")
    print(f"Diversification P99  : {format_money(report['diversification_benefit_p99'])}")

    print(f"\n{'Risk':<32}{'EAL':>14}{'P90':>14}{'P99':>14}{'P(loss)':>9}")
    for r in report["risks"]:
        print(f"{r['name'][:31]:<32}{format_money(r['mean'])}{format_money(r['p90'])}"
              f"{format_money(r['p99'])}{r['prob_any_loss']:>9.1%}")

    print(f"\n{'Business unit (incl. children)':<32}{'EAL':>14}{'P90':>14}{'P99':>14}")
    for u in sorted(report["business_units"], key=lambda u: u["rolled_up"]["mean"], reverse=True):
        ru = u["rolled_up"]
        if ru["mean"] > 0:
            print(f"{u['name'][:31]:<32}{format_money(ru['mean'])}{format_money(ru['p90'])}"
                  f"{format_money(ru['p99'])}")

# =========================================================================
# Main Program (Example Usage)
# =========================================================================

def main():
    """
    Builds an example organisation with quantified risks and simulates it.
    """
    log_message("Starting main program.")
    storage = JsonStorage(JSON_FILE)
    storage.clear_all_data()  # the example always starts from a clean file
    rm = RiskManager(storage)

    # Organisation
    hq = rm.create_business_unit("Headquarters", "The main office")
    finance = rm.create_business_unit("Finance", "Accounting and financial planning", hq.id)
    hr = rm.create_business_unit("Human Resources", "Employee management", hq.id)
    marketing = rm.create_business_unit("Marketing", "Marketing Department", hq.id)
    sales = rm.create_business_unit("Sales", "Sales Department", marketing.id)
    it = rm.create_business_unit("IT", "Information Technology", hq.id)
    development = rm.create_business_unit("Software Development", "Software Development", it.id)

    # Assets
    db1 = rm.create_asset(it.id, "Production Database", "virtual", "Primary database server", "critical")
    webapp1 = rm.create_asset(development.id, "Web Application", "virtual", "Customer facing web app", "medium")
    records = rm.create_asset(finance.id, "Financial Records", "information", "Balance sheets and P&L", "high")
    office = rm.create_asset(hq.id, "Office Space", "physical", "Main Headquarters Building", "medium")

    # People and responsibilities
    john = rm.create_person("John Smith", "CFO", "john.smith@example.com")
    jane = rm.create_person("Jane Doe", "CIO", "jane.doe@example.com")
    mike = rm.create_person("Mike Johnson", "HR Manager", "mike.johnson@example.com")
    robert = rm.create_person("Robert Williams", "Sales Manager", "robert.williams@example.com")
    david = rm.create_person("David Lee", "Software Architect", "david.lee@example.com")
    rm.assign_risk_owner(finance.id, john.id)
    rm.assign_risk_owner(it.id, jane.id)
    rm.assign_delegate(it.id, david.id)
    rm.assign_delegate(hr.id, mike.id)

    # Quantified risks (annual frequency x loss per event)
    rm.create_risk(
        it.id, "Ransomware attack",
        frequency={"type": "poisson", "rate": 0.15},
        impact={"dist": "lognorm_p10_p90", "p10": 200_000, "p90": 5_000_000},
        category="cyber", asset_ids=[db1.id], owner_id=jane.id,
        residual_frequency={"type": "poisson", "rate": 0.05},
        controls=["EDR on all endpoints", "Offline backups"],
    )
    rm.create_risk(
        it.id, "Database outage",
        frequency={"type": "poisson", "rate": 2.0},
        impact={"dist": "pert", "min": 5_000, "mode": 25_000, "max": 250_000},
        category="operational", asset_ids=[db1.id], owner_id=jane.id,
    )
    rm.create_risk(
        development.id, "Web app data breach",
        frequency={"type": "bernoulli", "p": 0.08},
        impact={"dist": "lognorm_p10_p90", "p10": 100_000, "p90": 3_000_000},
        category="cyber", asset_ids=[webapp1.id], owner_id=david.id,
        residual_impact={"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 1_500_000},
        controls=["Annual penetration test", "WAF"],
    )
    rm.create_risk(
        finance.id, "Payment fraud",
        frequency={"type": "poisson", "rate": 0.5},
        impact={"dist": "lognorm_p10_p90", "p10": 20_000, "p90": 400_000},
        category="financial", asset_ids=[records.id], owner_id=john.id,
    )
    rm.create_risk(
        hr.id, "Loss of key personnel",
        frequency={"type": "poisson", "rate": 1.0},
        impact={"dist": "triangular", "min": 30_000, "mode": 80_000, "max": 300_000},
        category="operational", owner_id=mike.id,
    )
    rm.create_risk(
        sales.id, "Major customer churn",
        frequency={"type": "bernoulli", "p": 0.2},
        impact={"dist": "pert", "min": 200_000, "mode": 600_000, "max": 2_000_000},
        category="strategic", owner_id=robert.id,
    )
    rm.create_risk(
        hq.id, "Fire at headquarters",
        frequency={"type": "bernoulli", "p": 0.01},
        impact={"dist": "lognorm_p10_p90", "p10": 1_000_000, "p90": 20_000_000},
        category="hazard", asset_ids=[office.id],
    )

    # Simulate inherent vs. residual risk with the same seed
    inherent = rm.simulate(n_trials=50_000, seed=42, residual=False).report()
    residual = rm.simulate(n_trials=50_000, seed=42, residual=True).report()
    print_report(inherent, "Inherent risk")
    print_report(residual, "Residual risk (after controls)")
    saved = inherent["total"]["mean"] - residual["total"]["mean"]
    print(f"\nControls reduce expected annual loss by {saved:,.0f}")

    report_file = rm.save_simulation_report({"inherent": inherent, "residual": residual})
    print(f"Full report saved to {report_file}")
    log_message("Program finished.")


if __name__ == "__main__":
    main()
