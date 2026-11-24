"""Example organisation with quantified risks, used by `quanterm demo`."""

from .risk_service import RiskService


def seed_demo(svc: RiskService) -> None:
    """Replaces all data with the demo organisation."""
    svc.repo.clear()

    hq = svc.create_business_unit("Headquarters", "The main office")
    finance = svc.create_business_unit("Finance", "Accounting and financial planning", hq.id)
    hr = svc.create_business_unit("Human Resources", "Employee management", hq.id)
    marketing = svc.create_business_unit("Marketing", "Marketing Department", hq.id)
    sales = svc.create_business_unit("Sales", "Sales Department", marketing.id)
    it = svc.create_business_unit("IT", "Information Technology", hq.id)
    dev = svc.create_business_unit("Software Development", "Software Development", it.id)

    db = svc.create_asset(it.id, "Production Database", "virtual", "Primary database server", "critical")
    webapp = svc.create_asset(dev.id, "Web Application", "virtual", "Customer facing web app", "medium")
    records = svc.create_asset(finance.id, "Financial Records", "information", "Balance sheets and P&L", "high")
    office = svc.create_asset(hq.id, "Office Space", "physical", "Main Headquarters Building", "medium")

    john = svc.create_person("John Smith", "CFO", "john.smith@example.com")
    jane = svc.create_person("Jane Doe", "CIO", "jane.doe@example.com")
    mike = svc.create_person("Mike Johnson", "HR Manager", "mike.johnson@example.com")
    robert = svc.create_person("Robert Williams", "Sales Manager", "robert.williams@example.com")
    david = svc.create_person("David Lee", "Software Architect", "david.lee@example.com")
    svc.assign_risk_owner(finance.id, john.id)
    svc.assign_risk_owner(it.id, jane.id)
    svc.add_delegate(it.id, david.id)
    svc.add_delegate(hr.id, mike.id)

    svc.create_risk(
        it.id, "Ransomware attack",
        frequency={"type": "poisson", "rate": 0.15},
        impact={"dist": "lognorm_p10_p90", "p10": 200_000, "p90": 5_000_000},
        category="cyber", asset_ids=[db.id], owner_id=jane.id,
        residual_frequency={"type": "poisson", "rate": 0.05},
        controls=["EDR on all endpoints", "Offline backups"],
    )
    svc.create_risk(
        it.id, "Database outage",
        frequency={"type": "poisson", "rate": 2.0},
        impact={"dist": "pert", "min": 5_000, "mode": 25_000, "max": 250_000},
        category="operational", asset_ids=[db.id], owner_id=jane.id,
    )
    svc.create_risk(
        dev.id, "Web app data breach",
        frequency={"type": "bernoulli", "p": 0.08},
        impact={"dist": "lognorm_p10_p90", "p10": 100_000, "p90": 3_000_000},
        category="cyber", asset_ids=[webapp.id], owner_id=david.id,
        residual_impact={"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 1_500_000},
        controls=["Annual penetration test", "WAF"],
    )
    svc.create_risk(
        finance.id, "Payment fraud",
        frequency={"type": "poisson", "rate": 0.5},
        impact={"dist": "lognorm_p10_p90", "p10": 20_000, "p90": 400_000},
        category="financial", asset_ids=[records.id], owner_id=john.id,
    )
    svc.create_risk(
        hr.id, "Loss of key personnel",
        frequency={"type": "poisson", "rate": 1.0},
        impact={"dist": "triangular", "min": 30_000, "mode": 80_000, "max": 300_000},
        category="operational", owner_id=mike.id,
    )
    svc.create_risk(
        sales.id, "Major customer churn",
        frequency={"type": "bernoulli", "p": 0.2},
        impact={"dist": "pert", "min": 200_000, "mode": 600_000, "max": 2_000_000},
        category="strategic", owner_id=robert.id,
    )
    svc.create_risk(
        hq.id, "Fire at headquarters",
        frequency={"type": "bernoulli", "p": 0.01},
        impact={"dist": "lognorm_p10_p90", "p10": 1_000_000, "p90": 20_000_000},
        category="hazard", asset_ids=[office.id],
    )
