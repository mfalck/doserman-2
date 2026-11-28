"""Example organisation with quantified risks, used by `riskmesh demo`."""

from datetime import datetime, timedelta

from .risk_service import RiskService


def seed_demo(svc: RiskService) -> None:
    """Replaces all data with the demo organisation."""
    svc.reset_all()

    hq = svc.create_business_unit("Headquarters", "The main office", code="HQ", organization_type="COMPANY",
                                  lei="DEMO00RISKMESH000138", country="DK")  # fictitious LEI
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
                                     data_locations=["DE"], certifications=["ISO 27001"],
                                     lei="DEMO00HYPERSCALEDC02", parent_company="Hyperscale Group SE")
    hosting = svc.create_supplier("Nordic Cloud A/S", tier="CRITICAL", country="DK", owner_id=jane.id,
                                  description="Hosting of production systems", contract_start="2024-01-01",
                                  contract_end="2027-12-31", next_review="2026-06-30",
                                  processes_personal_data=True, data_locations=["DK", "DE"],
                                  certifications=["ISO 27001", "ISAE 3402 type 2"],
                                  subcontractor_ids=[datacenter.id], lei="DEMO00NORDICCLOUD134",
                                  company_number="DK12345678")
    mail = svc.create_supplier("SecureMail ApS", tier="IMPORTANT", country="DK", owner_id=jane.id,
                               description="E-mail and notification service", next_review="2027-03-01",
                               processes_personal_data=True, data_locations=["EU"], exit_plan=True,
                               certifications=["ISO 27001"], company_number="DK87654321")
    portal = svc.create_service(dev.id, "Citizen self-service portal", criticality="CRITICAL",
                                owner_id=david.id, asset_ids=[webapp.id, db.id],
                                supplier_ids=[hosting.id, mail.id], rto_hours=4, rpo_hours=1, outage_cost_per_hour=3_000,
                                description="SaaS portal our customers offer to their citizens",
                                ict_service_type="Software as a service (SaaS)", data_locations=["DK", "DE"])
    metering = svc.create_service(it.id, "Meter data hub", criticality="HIGH", owner_id=jane.id,
                                  asset_ids=[db.id], supplier_ids=[hosting.id], rto_hours=8, rpo_hours=0.25,
                                  outage_cost_per_hour=2_000, ict_service_type="Data management and hosting",
                                  data_locations=["DK", "DE"])
    payroll = svc.create_service(finance.id, "Payroll processing", criticality="MEDIUM", owner_id=john.id)
    svc.create_customer("North Municipality", sector="municipal", regulations=["NIS2_IMPORTANT", "GDPR"],
                        service_ids=[portal.id, payroll.id], contract_value=1_800_000, notification_hours=24,
                        audit_rights=True, account_owner_id=robert.id, contract_end="2028-06-30",
                        penalty_per_hour=2_500, penalty_grace_hours=8, penalty_cap=150_000)
    svc.create_customer("South Energy Grid", sector="energy", regulations=["NIS2_ESSENTIAL", "CER"],
                        service_ids=[metering.id], contract_value=3_200_000, notification_hours=12,
                        audit_rights=True, exit_support=True, account_owner_id=robert.id,
                        penalty_per_hour=10_000, penalty_grace_hours=2, penalty_cap=500_000)
    svc.create_customer("East Savings Bank", sector="finance", regulations=["DORA", "GDPR"],
                        service_ids=[portal.id], contract_value=2_400_000, critical_function=True,
                        notification_hours=4, audit_rights=True, account_owner_id=robert.id,
                        penalty_per_hour=8_000, penalty_grace_hours=4, penalty_cap=400_000)

    ransomware = svc.create_risk(
        it.id, "Ransomware attack",
        frequency={"type": "poisson", "rate": 0.15},
        impact={"dist": "lognorm_p10_p90", "p10": 200_000, "p90": 5_000_000},
        category="cyber", asset_ids=[db.id], owner_id=jane.id,
        residual_frequency={"type": "poisson", "rate": 0.05}, service_ids=[portal.id, metering.id],
        evidence_categories=["ransomware"],
    )
    outage = svc.create_risk(
        it.id, "Database outage",
        frequency={"type": "poisson", "rate": 2.0},
        impact={"dist": "pert", "min": 5_000, "mode": 25_000, "max": 250_000},
        category="operational", asset_ids=[db.id], owner_id=jane.id,
        supplier_ids=[hosting.id],
        outage_hours={"dist": "lognorm_p10_p90", "p10": 1, "p90": 24},
        residual_outage_hours={"dist": "lognorm_p10_p90", "p10": 0.5, "p90": 8},
        evidence_categories=["hosting outage"],
    )
    breach = svc.create_risk(
        dev.id, "Web app data breach",
        frequency={"type": "bernoulli", "p": 0.08},
        impact={"dist": "lognorm_p10_p90", "p10": 100_000, "p90": 3_000_000},
        category="cyber", asset_ids=[webapp.id], owner_id=david.id, service_ids=[portal.id],
        residual_impact={"dist": "lognorm_p10_p90", "p10": 50_000, "p90": 1_500_000},
        evidence_categories=["data breach"],
    )
    fraud = svc.create_risk(
        finance.id, "Payment fraud",
        frequency={"type": "poisson", "rate": 0.5},
        impact={"dist": "lognorm_p10_p90", "p10": 20_000, "p90": 400_000},
        category="financial", asset_ids=[records.id], owner_id=john.id, evidence_categories=["payment fraud"],
    )
    svc.create_risk(
        hr.id, "Loss of key personnel",
        frequency={"type": "poisson", "rate": 1.0},
        impact={"dist": "triangular", "min": 30_000, "mode": 80_000, "max": 300_000},
        category="operational", owner_id=mike.id,
    )
    churn = svc.create_risk(
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

    # What we must achieve, and which risks threaten it (through the services that deliver it, or directly)
    svc.create_objective("Citizen portal available to our municipal customers", criticality="MISSION_CRITICAL",
                         service_ids=[portal.id], owner_id=david.id, kpi="Availability at least 99.9% per month",
                         tolerance_loss=1_000_000, tolerance_probability=0.05)
    svc.create_objective("Meter data delivered to the energy market on time", criticality="MISSION_CRITICAL",
                         service_ids=[metering.id], owner_id=jane.id, kpi="99.5% of daily deliveries before 10:00",
                         tolerance_loss=750_000, tolerance_probability=0.10)
    svc.create_objective("Correct and timely payroll", criticality="IMPORTANT", service_ids=[payroll.id],
                         owner_id=john.id)
    growth = svc.create_objective("Grow sales 10% in 2027", criticality="IMPORTANT", business_unit_id=sales.id,
                                  owner_id=robert.id, kpi="Revenue growth", target_date="2027-12-31")
    svc.update_risk(churn.id, objective_ids=[growth.id])
    svc.update_risk(breach.id, objective_ids=[growth.id])

    # Common causes: change a driver's level to see what an intervention would do (Drivers page)
    hygiene = svc.create_driver("Patch and access hygiene", owner_id=jane.id, uncertainty=1.4,
                                indicator="% of critical patches installed within 14 days",
                                description="How quickly we patch and how tightly we control privileged access")
    svc.create_driver_effect(hygiene.id, ransomware.id, frequency_sensitivity=1.2,
                             rationale="Most ransomware enters through unpatched systems or stolen credentials")
    svc.create_driver_effect(hygiene.id, breach.id, frequency_sensitivity=1.0)
    economy = svc.create_driver("Economic climate", owner_id=john.id, uncertainty=1.5,
                                indicator="Customer budget growth; business confidence index",
                                description="A downturn makes customers leave and fraudsters more active")
    svc.create_driver_effect(economy.id, churn.id, frequency_sensitivity=1.5)
    svc.create_driver_effect(economy.id, fraud.id, frequency_sensitivity=0.8, impact_sensitivity=0.5)
    platform = svc.create_driver("Hosting platform stability", owner_id=jane.id, uncertainty=1.3,
                                 indicator="Unplanned downtime minutes per month at Nordic Cloud")
    svc.create_driver_effect(platform.id, outage.id, frequency_sensitivity=1.0, impact_sensitivity=0.5)

    # A control that works through a driver: all risks the driver drives benefit
    svc.create_control(it.id, "Automated patching and privileged access management", control_type="PREVENTIVE",
                       owner_id=jane.id, implementation_cost=180_000, annual_cost=70_000, driver_id=hygiene.id,
                       driver_reduction=0.35, description="Patches within 72 hours; no standing admin rights")

    # Scenarios: what if the world turns? (compared with today on the total, appetite and objectives)
    svc.create_scenario("Geopolitical crisis", likelihood="plausible within 5 years",
                        description="State-backed cyber campaigns and a recession at the same time",
                        driver_levels={economy.id: 1.5, hygiene.id: 1.3},
                        risk_factors={ransomware.id: {"frequency": 3.0}, breach.id: {"frequency": 2.0}})
    svc.create_scenario("Data centre down for two days", likelihood="once in 20 years",
                        description="Power and cooling failure at the data centre behind Nordic Cloud",
                        outage_supplier_id=datacenter.id, outage_hours=48)
    svc.create_scenario("Patch programme delivered", likelihood="our plan for 2027",
                        description="Automated patching and privileged access management in place",
                        driver_levels={hygiene.id: 0.6})

    # Bad years that tend to come together, and what the board accepts
    svc.create_correlation(ransomware.id, breach.id, 0.5,
                           rationale="Same attackers and the same weaknesses in patching and access control")
    svc.create_correlation(ransomware.id, outage.id, 0.3, rationale="Shared database and hosting platform")
    svc.create_correlation(breach.id, churn.id, 0.4, rationale="Customers leave after a public data breach")
    svc.create_appetite("Group risk appetite", owner_id=john.id,
                        points=[{"loss": 1_000_000, "probability": 0.25}, {"loss": 5_000_000, "probability": 0.05},
                                {"loss": 20_000_000, "probability": 0.005}],
                        statement="We accept a bad year, but losing more than 5m should happen at most "
                                  "once in 20 years and more than 20m at most once in 200 years.")
    svc.create_appetite("IT risk tolerance", business_unit_id=it.id, owner_id=jane.id,
                        points=[{"loss": 500_000, "probability": 0.1}, {"loss": 2_000_000, "probability": 0.02}],
                        statement="IT losses above 2m at most once in 50 years.")

    # Incidents: one handled last month, one running now (its deadlines run from the moment the demo is loaded)
    svc.create_incident("Hosting outage, portal down 6 hours", "2026-10-21T07:40", status="CLOSED",
                        supplier_ids=[hosting.id], nis2_significant=False, resolved_at="2026-10-21T13:45",
                        owner_id=jane.id, root_cause="Failed storage firmware update at the hosting provider",
                        financial_impact=48_000,
                        notifications={f"customer:{c.id}": "2026-10-21T09:15" for c in svc.list_customers()
                                       if portal.id in c.service_ids})
    started = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    svc.create_incident("Data centre power loss", started, status="CONTAINED", supplier_ids=[datacenter.id],
                        nis2_significant=True, owner_id=jane.id,
                        description="Power loss at the data centre behind Nordic Cloud; portal and meter hub down",
                        notifications={"nis2:early_warning": (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")})

    # Evidence: what we have recorded since January 2025, and public figures (illustrative only:
    # replace them with real sources such as a national statistics office or the data protection authority)
    for d, n, loss in (("2025-02-11", 1, 18_000), ("2025-05-03", 2, 41_000), ("2025-09-22", 1, 9_500),
                       ("2026-01-15", 1, 64_000), ("2026-04-08", 1, 12_000), ("2026-08-30", 1, 27_000)):
        svc.create_observation(d, "hosting outage", count=n, loss=loss, supplier_id=hosting.id,
                               source="Incident log")
    svc.create_observation("2025-06-17", "data breach", loss=85_000, business_unit_id=dev.id, source="Incident log",
                           description="Customer data exposed through a misconfigured API")
    svc.create_observation("2025-03-12", "payment fraud", loss=35_000, business_unit_id=finance.id,
                           source="Insurance claims")
    svc.create_observation("2026-08-02", "payment fraud", loss=60_000, business_unit_id=finance.id,
                           source="Insurance claims")
    svc.create_benchmark("Illustrative: data breaches in mid-size companies", "data breach",
                         source="Example figure - replace with a real source", year=2025,
                         events_per_year=0.12, loss_p10=40_000, loss_p90=1_200_000, weight=2)
    svc.create_benchmark("Illustrative: ransomware against mid-size companies", "ransomware",
                         source="Example figure - replace with a real source", year=2025,
                         events_per_year=0.04, loss_p10=250_000, loss_p90=4_000_000, weight=3)

    # Controls and their tests
    edr = svc.create_control(it.id, "EDR on all endpoints", control_type="PREVENTIVE",
                             implementation_status="IMPLEMENTED", effectiveness_rating=4,
                             owner_id=jane.id, risk_ids=[ransomware.id],
                             implementation_cost=150_000, annual_cost=90_000, frequency_reduction=0.6)
    backups = svc.create_control(it.id, "Offline backups", control_type="CORRECTIVE",
                                 implementation_status="IMPLEMENTED", effectiveness_rating=4,
                                 owner_id=jane.id, risk_ids=[ransomware.id],
                                 implementation_cost=80_000, annual_cost=40_000, impact_reduction=0.5)
    pentest = svc.create_control(dev.id, "Annual penetration test", control_type="DETECTIVE",
                                 implementation_status="IMPLEMENTED", effectiveness_rating=3,
                                 owner_id=david.id, risk_ids=[breach.id], annual_cost=60_000,
                                 frequency_reduction=0.3)
    svc.create_control(dev.id, "Web application firewall", control_type="PREVENTIVE",
                       implementation_status="IN_PROGRESS", owner_id=david.id, risk_ids=[breach.id],
                       implementation_cost=60_000, annual_cost=25_000, frequency_reduction=0.4)
    four_eyes = svc.create_control(finance.id, "Four-eyes approval of payments", control_type="PREVENTIVE",
                                   implementation_status="IMPLEMENTED", effectiveness_rating=3,
                                   owner_id=john.id, risk_ids=[fraud.id], annual_cost=25_000,
                                   frequency_reduction=0.5)
    # Candidates: what would we get for the money? (Analysis > investment plan)
    svc.create_control(it.id, "Database high-availability cluster", control_type="CORRECTIVE",
                       owner_id=jane.id, risk_ids=[outage.id], implementation_cost=250_000, annual_cost=60_000,
                       impact_reduction=0.6, description="Fail-over within minutes instead of hours")
    svc.create_control(it.id, "Second hosting provider", control_type="CORRECTIVE", owner_id=jane.id,
                       risk_ids=[outage.id], implementation_cost=400_000, annual_cost=120_000,
                       frequency_reduction=0.3, impact_reduction=0.3, description="Also the exit plan DORA asks for")
    svc.create_control(finance.id, "Call-back verification of new payees", control_type="PREVENTIVE",
                       owner_id=john.id, risk_ids=[fraud.id], implementation_cost=20_000, annual_cost=15_000,
                       frequency_reduction=0.6)
    svc.create_control(hq.id, "Phishing awareness training", control_type="DIRECTIVE", owner_id=mike.id,
                       risk_ids=[ransomware.id, breach.id], implementation_cost=10_000, annual_cost=30_000,
                       frequency_reduction=0.25)

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
