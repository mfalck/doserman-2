'''
import json
from neo4j import GraphDatabase
from uuid import uuid4
from datetime import datetime
import logging

# =========================================================================
# Configuration
# =========================================================================

NEO4J_URI = "bolt://localhost:7687"  # Default Neo4j URI, change as needed
NEO4J_USER = "neo4j"  # Default username, change as needed
NEO4J_PASSWORD = "password"  # Default password, change as needed
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
# Neo4j Connection
# =========================================================================

class Neo4jClient:
    """
    Manages the connection to the Neo4j database.
    """
    def __init__(self, uri, user, password):
        self.uri = uri
        self.user = user
        self.password = password
        self.driver = None  # Initialize driver to None

    def connect(self):
        """
        Establishes a connection to the Neo4j database.
        """
        try:
            self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
            log_message("Successfully connected to Neo4j database.")
        except Exception as e:
            log_message(f"Error connecting to Neo4j: {e}", logging.ERROR)
            raise  # Re-raise the exception to be handled by the caller

    def close(self):
        """
        Closes the connection to the Neo4j database.
        """
        if self.driver:
            self.driver.close()
            log_message("Closed connection to Neo4j database.")

    def get_session(self):
        """
        Returns a session.  Checks if the driver exists.
        """
        if not self.driver:
            self.connect()  # Connect if the driver doesn't exist
        return self.driver.session()

    def execute_query(self, query, parameters=None):
        """
        Executes a Cypher query.  Handles connection errors.
        """
        try:
            with self.get_session() as session: # Use get_session()
                result = session.run(query, parameters)
                return result.data()
        except Exception as e:
            log_message(f"Error executing query: {query} with params: {parameters}. Error: {e}", logging.ERROR)
            raise  # Re-raise to be handled by caller

    def ensure_constraints(self):
        """
        Ensures the necessary constraints and indexes exist in the database.
        """
        try:
            self.execute_query("CREATE CONSTRAINT IF NOT EXISTS ON (bu:BusinessUnit) ASSERT bu.id IS UNIQUE")
            self.execute_query("CREATE CONSTRAINT IF NOT EXISTS ON (asset:Asset) ASSERT asset.id IS UNIQUE")
            self.execute_query("CREATE CONSTRAINT IF NOT EXISTS ON (person:Person) ASSERT person.id IS UNIQUE")
            self.execute_query("CREATE INDEX IF NOT EXISTS FOR (bu:BusinessUnit) ON (bu.name)")
            self.execute_query("CREATE INDEX IF NOT EXISTS FOR (asset:Asset) ON (asset.name)")
            self.execute_query("CREATE INDEX IF NOT EXISTS FOR (person:Person) ON (person.name)")
            log_message("Successfully ensured constraints and indexes.")
        except Exception as e:
            log_message(f"Error ensuring constraints: {e}", logging.ERROR)
            raise  # Important: Re-raise so the application knows about the failure

# Global instance
neo4j_client = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

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
        self.assets = []  # List of Asset objects
        self.risk_owner = None  # Person object
        self.delegates = []  # List of Person objects

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
            "assets": [asset.to_dict() for asset in self.assets],
            "risk_owner_id": self.risk_owner.id if self.risk_owner else None,
            "delegate_ids": [delegate.id for delegate in self.delegates],
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

# =========================================================================
# Business Layer Functions
# =========================================================================

class RiskManager:
    """
    Handles the business logic for managing the organization structure and risk data.
    """
    def __init__(self, neo4j_client):
        """
        Initializes the RiskManager with a Neo4j client.
        """
        self.neo4j_client = neo4j_client

    def create_business_unit(self, name, description=None, parent_id=None):
        """
        Creates a new business unit.
        """
        if not name:
            raise ValueError("Business unit name cannot be empty.")
        # Check for duplicate name
        query = "MATCH (bu:BusinessUnit {name: $name}) RETURN bu"
        result = self.neo4j_client.execute_query(query, {"name": name})
        if result:
            raise ValueError(f"Business unit with name '{name}' already exists.")

        bu = BusinessUnit(name, description, parent_id)
        query = """
            CREATE (bu:BusinessUnit {
                id: $id,
                name: $name,
                description: $description,
                parent_id: $parent_id
            })
            RETURN bu
        """
        parameters = {
            "id": bu.id,
            "name": bu.name,
            "description": bu.description,
            "parent_id": bu.parent_id,
        }
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Created business unit: {bu}")
            return bu
        except Exception as e:
            log_message(f"Error creating business unit: {e}", logging.ERROR)
            raise

    def get_business_unit(self, unit_id):
        """
        Retrieves a business unit by its ID.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        query = """
            MATCH (bu:BusinessUnit {id: $id})
            OPTIONAL MATCH (bu)-[:OWNS_ASSET]->(asset:Asset)
            OPTIONAL MATCH (bu)-[:HAS_RISK_OWNER]->(owner:Person)
            OPTIONAL MATCH (bu)-[:HAS_DELEGATE]->(delegate:Person)
            RETURN bu,
                   COLLECT(asset) AS assets,
                   owner,
                   COLLECT(delegate) AS delegates
        """
        parameters = {"id": unit_id}
        result = self.neo4j_client.execute_query(query, parameters)
        if not result:
            return None  # Or raise an exception if you prefer
        data = result[0]['bu']
        assets = [Asset(**a) for a in result[0]['assets']] if result[0]['assets'][0] else []
        owner = Person(**result[0]['owner']) if result[0]['owner'] else None
        delegates = [Person(**d) for d in result[0]['delegates']] if result[0]['delegates'][0] else []

        bu = BusinessUnit(name=data['name'], description=data['description'], parent_id=data['parent_id'])
        bu.id = data['id']
        bu.assets = assets
        bu.risk_owner = owner
        bu.delegates = delegates
        return bu

    def update_business_unit(self, unit_id, name=None, description=None, parent_id=None):
        """
        Updates an existing business unit.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")

        # Fetch the existing business unit to ensure it exists and get its current data
        existing_bu = self.get_business_unit(unit_id)
        if not existing_bu:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")

        # Update only the provided fields
        if name is not None:
            existing_bu.name = name
        if description is not None:
            existing_bu.description = description
        if parent_id is not None:
            existing_bu.parent_id = parent_id

        query = """
            MATCH (bu:BusinessUnit {id: $id})
            SET bu.name = $name,
                bu.description = $description,
                bu.parent_id = $parent_id
            RETURN bu
        """
        parameters = {
            "id": existing_bu.id,
            "name": existing_bu.name,
            "description": existing_bu.description,
            "parent_id": existing_bu.parent_id,
        }

        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Updated business unit: {existing_bu}")
            return existing_bu # Return the updated object
        except Exception as e:
            log_message(f"Error updating business unit: {e}", logging.ERROR)
            raise

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

        query = """
            MATCH (bu:BusinessUnit {id: $id})
            DETACH DELETE bu
        """
        parameters = {"id": unit_id}
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Deleted business unit with ID: {unit_id}")
        except Exception as e:
            log_message(f"Error deleting business unit: {e}", logging.ERROR)
            raise

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
        query = """
            CREATE (asset:Asset {
                id: $id,
                name: $name,
                asset_type: $asset_type,
                description: $description,
                criticality: $criticality
            })
            WITH asset
            MATCH (bu:BusinessUnit {id: $unit_id})
            CREATE (bu)-[:OWNS_ASSET]->(asset)
            RETURN asset
        """
        parameters = {
            "id": asset.id,
            "name": asset.name,
            "asset_type": asset.asset_type,
            "description": asset.description,
            "criticality": asset.criticality,
            "unit_id": unit_id,
        }
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Created asset: {asset} for business unit: {unit_id}")
            return asset
        except Exception as e:
            log_message(f"Error creating asset: {e}", logging.ERROR)
            raise

    def get_asset(self, asset_id):
        """
        Retrieves an asset by its ID.
        """
        if not asset_id:
            raise ValueError("Asset ID cannot be empty.")
        query = """
            MATCH (asset:Asset {id: $id})
            RETURN asset
        """
        parameters = {"id": asset_id}
        result = self.neo4j_client.execute_query(query, parameters)
        if not result:
            return None
        asset_data = result[0]['asset']
        asset = Asset(name=asset_data['name'], asset_type=asset_data['asset_type'], description=asset_data['description'], criticality=asset_data['criticality'])
        asset.id = asset_data['id']
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
        if name is not None:
            existing_asset.name = name
        if asset_type is not None:
            existing_asset.asset_type = asset_type
        if description is not None:
            existing_asset.description = description
        if criticality is not None:
            existing_asset.criticality = criticality

        query = """
            MATCH (asset:Asset {id: $id})
            SET asset.name = $name,
                asset.asset_type = $asset_type,
                asset.description = $description,
                asset.criticality = $criticality
            RETURN asset
        """
        parameters = {
            "id": existing_asset.id,
            "name": existing_asset.name,
            "asset_type": existing_asset.asset_type,
            "description": existing_asset.description,
            "criticality": existing_asset.criticality,
        }

        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Updated asset: {existing_asset}")
            return existing_asset
        except Exception as e:
            log_message(f"Error updating asset: {e}", logging.ERROR)
            raise

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
        query = """
            MATCH (asset:Asset {id: $id})
            DETACH DELETE asset
        """
        parameters = {"id": asset_id}
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Deleted asset with ID: {asset_id}")
        except Exception as e:
            log_message(f"Error deleting asset: {e}", logging.ERROR)
            raise

    def create_person(self, name, title=None, email=None):
        """
        Creates a new person.
        """
        if not name:
            raise ValueError("Person name cannot be empty.")
        person = Person(name, title, email)
        query = """
            CREATE (person:Person {
                id: $id,
                name: $name,
                title: $title,
                email: $email
            })
            RETURN person
        """
        parameters = {
            "id": person.id,
            "name": person.name,
            "title": person.title,
            "email": person.email,
        }
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Created person: {person}")
            return person
        except Exception as e:
            log_message(f"Error creating person: {e}", logging.ERROR)
            raise

    def get_person(self, person_id):
        """
        Retrieves a person by their ID.
        """
        if not person_id:
            raise ValueError("Person ID cannot be empty.")
        query = """
            MATCH (person:Person {id: $id})
            RETURN person
        """
        parameters = {"id": person_id}
        result = self.neo4j_client.execute_query(query, parameters)
        if not result:
            return None
        person_data = result[0]['person']
        person = Person(name=person_data['name'], title=person_data['title'], email=person_data['email'])
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

        if name is not None:
            existing_person.name = name
        if title is not None:
            existing_person.title = title
        if email is not None:
            existing_person.email = email
        query = """
            MATCH (person:Person {id: $id})
            SET person.name = $name,
                person.title = $title,
                person.email = $email
            RETURN person
        """
        parameters = {
            "id": existing_person.id,
            "name": existing_person.name,
            "title": existing_person.title,
            "email": existing_person.email,
        }
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Updated person: {existing_person}")
            return existing_person
        except Exception as e:
            log_message(f"Error updating person: {e}", logging.ERROR)
            raise

    def delete_person(self, person_id):
        """
        Deletes a person by their ID.
        """
        if not person_id:
            raise ValueError("Person ID cannot be empty.")
        # Check if the person exists.
        existing_person = self.get_person(person_id)
        if not existing_person:
            raise ValueError(f"Person with ID '{person_id}' not found.")

        query = """
            MATCH (person:Person {id: $id})
            DETACH DELETE person
        """
        parameters = {"id": person_id}
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Deleted person with ID: {person_id}")
        except Exception as e:
            log_message(f"Error deleting person: {e}", logging.ERROR)
            raise

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
        query = """
            MATCH (bu:BusinessUnit {id: $unit_id})
            MATCH (person:Person {id: $person_id})
            MERGE (bu)-[:HAS_RISK_OWNER]->(person)
            RETURN bu, person
        """
        parameters = {"unit_id": unit_id, "person_id": person_id}
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Assigned {person} as risk owner for {business_unit}")
        except Exception as e:
            log_message(f"Error assigning risk owner: {e}", logging.ERROR)
            raise

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

        query = """
            MATCH (bu:BusinessUnit {id: $unit_id})
            MATCH (person:Person {id: $person_id})
            MERGE (bu)-[:HAS_DELEGATE]->(person)
            RETURN bu, person
        """
        parameters = {"unit_id": unit_id, "person_id": person_id}
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Assigned {person} as delegate for {business_unit}")
        except Exception as e:
            log_message(f"Error assigning delegate: {e}", logging.ERROR)
            raise

    def remove_delegate(self, unit_id, person_id):
        """
        Removes a person as a risk management delegate from a business unit.
        """
        if not unit_id:
            raise ValueError("Business unit ID cannot be empty.")
        if not person_id:raise ValueError("Person ID cannot be empty.")

        # Check if the business unit and person exist
        business_unit = self.get_business_unit(unit_id)
        if not business_unit:
            raise ValueError(f"Business unit with ID '{unit_id}' not found.")
        person = self.get_person(person_id)
        if not person:
            raise ValueError(f"Person with ID '{person_id}' not found.")

        query = """
            MATCH (bu:BusinessUnit {id: $unit_id})-[r:HAS_DELEGATE]->(person:Person {id: $person_id})
            DELETE r
        """
        parameters = {"unit_id": unit_id, "person_id": person_id}
        try:
            self.neo4j_client.execute_query(query, parameters)
            log_message(f"Removed delegate {person.name} from business unit {business_unit.name}")
        except Exception as e:
            log_message(f"Error removing delegate: {e}", logging.ERROR)
            raise

    def export_data(self):
        """
        Exports all data from the Neo4j database to a JSON file.
        """
        query = """
            MATCH (bu:BusinessUnit)
            OPTIONAL MATCH (bu)-[:OWNS_ASSET]->(asset:Asset)
            OPTIONAL MATCH (bu)-[:HAS_RISK_OWNER]->(owner:Person)
            OPTIONAL MATCH (bu)-[:HAS_DELEGATE]->(delegate:Person)
            RETURN bu,
                   COLLECT(asset) AS assets,
                   owner,
                   COLLECT(delegate) AS delegates
        """
        try:
            results = self.neo4j_client.execute_query(query)
            data = []
            for record in results:
                bu_data = record['bu']
                assets = [Asset(**a).to_dict() for a in record['assets']] if record['assets'][0] else []
                owner = Person(**record['owner']).to_dict() if record['owner'] else None
                delegates = [Person(**d).to_dict() for d in record['delegates']] if record['delegates'][0] else []
                bu = {
                    "id": bu_data['id'],
                    "name": bu_data['name'],
                    "description": bu_data['description'],
                    "parent_id": bu_data['parent_id'],
                    "assets": assets,
                    "risk_owner_id": owner['id'] if owner else None,
                    "delegate_ids": [d['id'] for d in delegates],
                }
                data.append(bu)
            # Include Person and Asset data in the export
            persons = [self.get_person(p['id']).to_dict() for p in self.neo4j_client.execute_query("MATCH (p:Person) RETURN p")]
            all_assets = [self.get_asset(a['id']).to_dict() for a in self.neo4j_client.execute_query("MATCH (a:Asset) RETURN a")]

            export_data = {
                "business_units": data,
                "persons": persons,
                "assets": all_assets
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
        Imports data from a JSON file into the Neo4j database.  This version
        clears the database before importing.  It also handles Persons
        and Assets.
        """
        try:
            with open(filename, "r") as f:
                import_data = json.load(f)

            # Clear existing data (use with caution!)
            self.neo4j_client.execute_query("MATCH (n) DETACH DELETE n")
            log_message("Cleared existing data in the database.")

            # Import Persons first
            persons = import_data.get("persons", [])  # handles if "persons" is missing
            person_map = {}  # for storing created Person objects
            for person_data in persons:
                person = self.create_person(
                    name=person_data['name'],
                    title=person_data.get('title'),  # use .get() with defaults
                    email=person_data.get('email')
                )
                person_map[person.id] = person  # store Person object

            # Import Assets next
            assets = import_data.get("assets", [])
            asset_map = {}
            for asset_data in assets:
                asset = Asset(
                    name=asset_data['name'],
                    asset_type=asset_data['asset_type'],
                    description=asset_data.get('description'),
                    criticality=asset_data.get('criticality')
                )
                # Directly insert the asset.
                query = """
                    CREATE (a:Asset {
                        id: $id,
                        name: $name,
                        asset_type: $asset_type,
                        description: $description,
                        criticality: $criticality
                    })
                    RETURN a
                    """
                parameters = {
                    "id": asset.id,
                    "name": asset.name,
                    "asset_type": asset.asset_type,
                    "description": asset.description,
                    "criticality": asset.criticality
                }
                self.neo4j_client.execute_query(query, parameters)
                asset_map[asset.id] = asset

            # Import Business Units and connect them
            business_units = import_data.get("business_units", []) # handles if business_units is missing.
            bu_map = {}  # Store created BusinessUnit objects
            for bu_data in business_units:
                bu = self.create_business_unit(
                    name=bu_data['name'],
                    description=bu_data.get('description'),
                    parent_id=bu_data.get('parent_id')
                )
                bu.id = bu_data['id'] #Preserve ID from import
                bu_map[bu.id] = bu # Store the BusinessUnit object
                # Create the assets and connect them.
                for asset_data in bu_data.get('assets', []):
                    asset_id = asset_data['id']
                    if asset_id in asset_map:
                        asset = asset_map[asset_id]
                        query = """
                            MATCH (bu:BusinessUnit {id: $bu_id})
                            MATCH (a:Asset {id: $asset_id})
                            CREATE (bu)-[:OWNS_ASSET]->(a)
                            """
                        parameters = {
                            "bu_id": bu.id,
                            "asset_id": asset.id
                        }
                        self.neo4j_client.execute_query(query, parameters)
                        bu.assets.append(asset)

                # Handle risk owner
                risk_owner_id = bu_data.get('risk_owner_id')
                if risk_owner_id and risk_owner_id in person_map:
                    owner = person_map[risk_owner_id]
                    self.assign_risk_owner(bu.id, risk_owner_id)
                    bu.risk_owner = owner

                # Handle delegates
                delegate_ids = bu_data.get('delegate_ids', [])
                for delegate_id in delegate_ids:
                    if delegate_id in person_map:
                        delegate = person_map[delegate_id]
                        self.assign_delegate(bu.id, delegate_id)
                        bu.delegates.append(delegate)
            log_message(f"Imported data from {filename}")
        except Exception as e:
            log_message(f"Error importing data: {e}", logging.ERROR)
            raise
    def get_organisation_structure(self):
        """
        Retrieves the organisation structure from the Neo4j database.

        Returns:
            list: A list of dictionaries representing the organisation structure.
                  Each dictionary contains the business unit's ID, name, and parent ID.
        """
        query = """
            MATCH (bu:BusinessUnit)
            RETURN bu.id AS id, bu.name AS name, bu.parent_id AS parent_id
        """
        try:
            results = self.neo4j_client.execute_query(query)
            # Convert the result to a list of dictionaries
            organisation_structure = [
                {"id": record["id"], "name": record["name"], "parent_id": record["parent_id"]}
                for record in results
            ]
            return organisation_structure
        except Exception as e:
            log_message(f"Error retrieving organisation structure: {e}", logging.ERROR)
            raise

# =========================================================================
# Main Program (Example Usage)
# =========================================================================

def main():
    """
    Main function to run the program.
    """
    try:
        neo4j_client.connect()
        neo4j_client.ensure_constraints() # Ensure constraints are created.
        risk_manager = RiskManager(neo4j_client)

        # Example usage:
        log_message("Starting main program.")
        # Create some business units
        try:
            hq = risk_manager.create_business_unit("Headquarters", "The main office")
            finance = risk_manager.create_business_unit("Finance", "Accounting and financial planning", hq.id)
            hr = risk_manager.create_business_unit("Human Resources", "Employee management", hq.id)
            marketing = risk_manager.create_business_unit("Marketing", "Marketing Department", hq.id)
            sales = risk_manager.create_business_unit("Sales", "Sales Department", marketing.id)
            it = risk_manager.create_business_unit("IT", "Information Technology", hq.id)
            development = risk_manager.create_business_unit("Software Development", "Software Development", it.id)


        except ValueError as e:
            log_message(f"Error creating business units: {e}", logging.ERROR)
            return  # Exit if there's an error creating business units

        # Create some assets
        try:
            server1 = risk_manager.create_asset(it.id, "Server Room 1", "physical", "Main server room", "high")
            db1 = risk_manager.create_asset(it.id, "Production Database", "virtual", "Primary database server", "critical")
            webapp1 = risk_manager.create_asset(development.id, "Web Application", "virtual", "Customer facing web app", "medium")
            contract1 = risk_manager.create_asset(finance.id, "Financial Records", "information", "Balance sheets and P&L", "high")
            office_space = risk_manager.create_asset(hq.id, "Office Space", "physical", "Main Headquarters Building", "medium")

        except ValueError as e:
            log_message(f"Error creating assets: {e}", logging.ERROR)
            return

        # Create some people
        try:
            john = risk_manager.create_person("John Smith", "CFO", "john.smith@example.com")
            jane = risk_manager.create_person("Jane Doe", "CIO", "jane.doe@example.com")
            mike = risk_manager.create_person("Mike Johnson", "HR Manager", "mike.johnson@example.com")
            alice = risk_manager.create_person("Alice Brown", "Marketing Director", "alice.brown@example.com")
            robert = risk_manager.create_person("Robert Williams", "Sales Manager", "robert.williams@example.com")
            david = risk_manager.create_person("David Lee", "Software Architect", "david.lee@example.com")
        except ValueError as e:
            log_message(f"Error creating people: {e}", logging.ERROR)
            return

        # Assign risk owners and delegates
        try:
            risk_manager.assign_risk_owner(finance.id, john.id)
            risk_manager.assign_risk_owner(it.id, jane.id)
            risk_manager.assign_delegate(it.id, david.id)
            risk_manager.assign_delegate(it.id, john.id) # John is also a delegate for IT.
            risk_manager.assign_delegate(hr.id, mike.id)
            risk_manager.remove_delegate(it.id, john.id)
        except ValueError as e:
            log_message(f"Error assigning risk owners/delegates: {e}", logging.ERROR)
            return

        # Example of updating a business unit
        try:
            updated_finance = risk_manager.update_business_unit(finance.id, name="Finance Department", description="Updated Finance Description")
            log_message(f"Updated Finance: {updated_finance}")
        except ValueError as e:
            log_message(f"Error updating business unit: {e}", logging.ERROR)

        # Example of deleting an asset
        try:
            risk_manager.delete_asset(server1.id)
            log_message(f"Deleted asset: {server1}")
        except ValueError as e:
            log_message(f"Error deleting asset: {e}", logging.ERROR)

        # Example of exporting data
        try:
            export_file = risk_manager.export_data()
            log_message(f"Data exported to: {export_file}")
        except Exception as e:
            log_message(f"Error during export: {e}", logging.ERROR)

        # Example of importing data
        try:
            risk_manager.import_data(export_file)  # Import the file we just exported
            log_message(f"Data imported from: {export_file}")
        except Exception as e:
            log_message(f"Error during import: {e}", logging.ERROR)
        
        # Example of getting org structure
        try:
            org_structure = risk_manager.get_organisation_structure()
            log_message(f"Organisation Structure: {org_structure}")
            print("Organisation Structure: ")
            print(json.dumps(org_structure, indent=4))
        except Exception as e:
            log_message(f"Error getting org structure: {e}", logging.ERROR)

    finally:
        neo4j_client.close()
        log_message("Program finished.")

if __name__ == "__main__":
    main()
'''