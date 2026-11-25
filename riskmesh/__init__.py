"""
RiskMesh - enterprise risk management with quantitative risk analysis.

Three tiers, each depending only on the one below:

  presentation/   REST API (web front end, e.g. Lovable) and CLI
       |
  services/       business logic: RiskService (+ domain/ models and risk_model)
       |
  storage/        Repository interface with JSON, SQL (SQLAlchemy) and Neo4j backends
"""

__version__ = "0.1.0"
