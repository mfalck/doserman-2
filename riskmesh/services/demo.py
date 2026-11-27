"""Example organisation with quantified risks, used by `riskmesh demo`."""

from .risk_service import RiskService


def seed_demo(svc: RiskService) -> None:
    """Replaces all data with the demo organisation."""
    svc.reset_all()

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

    # Third parties: our suppliers (with a fourth party), the services we deliver and our customers
    datacenter = svc.create_supplier("Hyperscale DC GmbH", tier="IMPORTANT", country="DE",
                                     description="Data centre used by our hosting provider",
                                     data_locations=["DE"], certifications=["ISO 27001"])
    hosting = svc.create_supplier("Nordic Cloud A/S", tier="CRITICAL", country="DK", owner_id=jane.id,
                                  description="Hosting of production systems", contract_start="2024-01-01",
                                  contract_end="2027-12-31", next_review="2026-06-30",
                                  processes_personal_data=True, data_locations=["DK", "DE"],
                                  certifications=["ISO 27001", "ISAE 3402 type 2"],
                                  subcontractor_ids=[datacenter.id])
    mail = svc.create_supplier("SecureMail ApS", tier="IMPORTANT", country="DK", owner_id=jane.id,
                               description="E-mail and notification service", next_review="2027-03-01",
                               processes_personal_data=True, data_locations=["EU"], exit_plan=True,
                               certifications=["ISO 27001"])
    portal = svc.create_service(dev.id, "Citizen self-service portal", criticality="CRITICAL",
                                owner_id=david.id, asset_ids=[webapp.id, db.id],
                                supplier_ids=[hosting.id, mail.id], rto_hours=4, rpo_hours=1,
                                description="SaaS portal our customers offer to their citizens")
    metering = svc.create_service(it.id, "Meter data hub", criticality="HIGH", owner_id=jane.id,
                                  asset_ids=[db.id], supplier_ids=[hosting.id], rto_hours=8, rpo_hours=0.25)
    payroll = svc.create_service(finance.id, "Payroll processing", criticality="MEDIUM", owner_id=john.id)
    svc.create_customer("North Municipality", sector="municipal", regulations=["NIS2_IMPORTANT", "GDPR"],
                        service_ids=[portal.id, payroll.id], contract_value=1_800_000, notification_hours=24,
                        audit_rights=True, account_owner_id=robert.id, contract_end="2028-06-30")
    svc.create_customer("South Energy Grid", sector="energy", regulations=["NIS2_ESSENTIAL", "CER"],
                        service_ids=[metering.id], contract_value=3_200_000, notification_hours=12,
                        audit_rights=True, exit_support=True, account_owner_id=robert.id)
    svc.create_customer("East Savings Bank", sector="finance", regulations=["DORA", "GDPR"],
                        service_ids=[portal.id], contract_value=2_400_000, critical_function=True,
                        notification_hours=4, audit_rights=True, account_owner_id=robert.id)

    ransomware = svc.create_risk(
        it.id, "Ransomware attack",
        frequency={"type": "poisson", "rate": 0.15},
        impact={"dist": "lognorm_p10_p90", "p10": 200_000, "p90": 5_000_000},
        category="cyber", asset_ids=[db.id], owner_id=jane.id,
        residual_frequency={"type": "poisson", "rate": 0.05}, service_ids=[portal.id, metering.id],
    )
    svc.create_risk(
        it.id, "Database outage",
        frequency={"type": "poisson", "rate": 2.0},
        impact={"dist": "pert", "min": 5_000, "mode": 25_000, "max": 250_000},
        category="operational", asset_ids=[db.id], owner_id=jane.id,
        service_ids=[portal.id, metering.id], supplier_ids=[hosting.id],
    )
    breach = svc.create_risk(
        dev.id, "Web app data breach",
        frequency={"type": "bernoulli", "p": 0.08},
        impact={"dist": "lognorm_p10_p90", "p10": 100_000, "p90": 3_000_000},
        category="cyber", asset_ids=[webapp.id], owner_id=david.id, service_ids=[portal.id],
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
