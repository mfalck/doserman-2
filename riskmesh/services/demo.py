"""Example organisation with quantified risks, used by `riskmesh demo`."""

from .risk_service import RiskService


def seed_demo(svc: RiskService) -> None:
    """Replaces all data with the demo organisation."""
    svc.repo.clear()

    hq = svc.create_business_unit("Headquarters", "The main office", code="HQ", organization_type="COMPANY")
    finance = svc.create_business_unit("Finance", "Accounting and financial planning", hq.id, code="FIN")
    hr = svc.create_business_unit("Human Resources", "Employee management", hq.id, code="HR")
    marketing = svc.create_business_unit("Marketing", "Marketing Department", hq.id, code="MKT")
    sales = svc.create_business_unit("Sales", "Sales Department", marketing.id, code="SAL")
    it = svc.create_business_unit("IT", "Information Technology", hq.id, code="IT")
    dev = svc.create_business_unit("Software Development", "Software Development", it.id, code="DEV")

    db = svc.create_asset(it.id, "Production Database", "virtual", "Primary database server", "critical", value=400_000)
    webapp = svc.create_asset(dev.id, "Web Application", "virtual", "Customer facing web app", "medium", value=250_000)
    records = svc.create_asset(finance.id, "Financial Records", "information", "Balance sheets and P&L", "high", value=50_000)
    office = svc.create_asset(hq.id, "Office Space", "physical", "Main Headquarters Building", "medium", value=12_000_000)

    john = svc.create_person("John Smith", "CFO", "john.smith@example.com")
    jane = svc.create_person("Jane Doe", "CIO", "jane.doe@example.com")
    mike = svc.create_person("Mike Johnson", "HR Manager", "mike.johnson@example.com")
    robert = svc.create_person("Robert Williams", "Sales Manager", "robert.williams@example.com")
    david = svc.create_person("David Lee", "Software Architect", "david.lee@example.com")
    svc.assign_risk_owner(finance.id, john.id)
    svc.assign_risk_owner(it.id, jane.id)
    svc.add_delegate(it.id, david.id)
    svc.add_delegate(hr.id, mike.id)

    ransomware = svc.create_risk(
        it.id, "Ransomware attack",
        frequency={"type": "poisson", "rate": 0.15},
        impact={"dist": "lognorm_p10_p90", "p10": 200_000, "p90": 5_000_000},
        category="cyber", asset_ids=[db.id], owner_id=jane.id,
        residual_frequency={"type": "poisson", "rate": 0.05},
    )
    svc.create_risk(
        it.id, "Database outage",
        frequency={"type": "poisson", "rate": 2.0},
        impact={"dist": "pert", "min": 5_000, "mode": 25_000, "max": 250_000},
        category="operational", asset_ids=[db.id], owner_id=jane.id,
    )
    breach = svc.create_risk(
        dev.id, "Web app data breach",
        frequency={"type": "bernoulli", "p": 0.08},
        impact={"dist": "lognorm_p10_p90", "p10": 100_000, "p90": 3_000_000},
        category="cyber", asset_ids=[webapp.id], owner_id=david.id,
        residual_impact={"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 1_500_000},
    )
    fraud = svc.create_risk(
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

    # Controls and their tests
    edr = svc.create_control(it.id, "EDR on all endpoints", control_type="PREVENTIVE",
                             implementation_status="IMPLEMENTED", effectiveness_rating=4,
                             owner_id=jane.id, risk_ids=[ransomware.id])
    backups = svc.create_control(it.id, "Offline backups", control_type="CORRECTIVE",
                                 implementation_status="IMPLEMENTED", effectiveness_rating=4,
                                 owner_id=jane.id, risk_ids=[ransomware.id])
    pentest = svc.create_control(dev.id, "Annual penetration test", control_type="DETECTIVE",
                                 implementation_status="IMPLEMENTED", effectiveness_rating=3,
                                 owner_id=david.id, risk_ids=[breach.id])
    svc.create_control(dev.id, "Web application firewall", control_type="PREVENTIVE",
                       implementation_status="IN_PROGRESS", owner_id=david.id, risk_ids=[breach.id])
    four_eyes = svc.create_control(finance.id, "Four-eyes approval of payments", control_type="PREVENTIVE",
                                   implementation_status="IMPLEMENTED", effectiveness_rating=3,
                                   owner_id=john.id, risk_ids=[fraud.id])

    svc.create_control_event(edr.id, "2026-09-15", event_type="TEST", status="PASSED",
                             description="Quarterly EDR coverage check: 99.6% of endpoints")
    svc.create_control_event(backups.id, "2026-10-02", event_type="TEST", status="PARTIAL",
                             description="Restore test succeeded, but took 30 hours (target 24)")
    svc.create_control_event(pentest.id, "2026-06-20", event_type="AUDIT", status="PASSED",
                             description="External penetration test, no critical findings")
    svc.create_control_event(four_eyes.id, "2026-08-11", event_type="REVIEW", status="FAILED",
                             description="3 of 40 sampled payments approved by one person only")

    # Loss events
    svc.create_risk_event(fraud.id, "CEO-fraud attempt", "2026-03-04", event_type="NEAR_MISS",
                          status="CLOSED", description="Fake invoice detected by the bank",
                          financial_impact=0)
    svc.create_risk_event(fraud.id, "Duplicate supplier payment", "2026-08-02", event_type="INCIDENT",
                          status="CLOSED", description="Invoice paid twice, partly recovered",
                          impact_description="Net loss after recovery", financial_impact=18_500)
    svc.create_risk_event(ransomware.id, "Phishing with malware attachment", "2026-10-21",
                          event_type="INCIDENT", status="INVESTIGATING",
                          description="One laptop infected, isolated by EDR", financial_impact=4_200)
