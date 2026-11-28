"""Controls, events, cascades, audit trail and derived scores - every backend."""

import pytest

from riskmesh.domain.errors import NotFoundError, ValidationError
from riskmesh.domain.models import AuditEntry, ControlEvent, RiskEvent
from riskmesh.services.demo import seed_demo

FREQ = {"type": "poisson", "rate": 1.0}
IMPACT = {"dist": "fixed", "value": 1000}


@pytest.fixture
def setup(svc):
    bu = svc.create_business_unit("Ops")
    risk = svc.create_risk(bu.id, "Outage", FREQ, IMPACT)
    control = svc.create_control(bu.id, "Monitoring", risk_ids=[risk.id], implementation_status="IMPLEMENTED")
    return svc, bu, risk, control


def test_control_validation(setup):
    svc, bu, risk, _ = setup
    with pytest.raises(ValidationError):
        svc.create_control(bu.id, "x", control_type="MAGIC")
    with pytest.raises(ValidationError):
        svc.create_control(bu.id, "x", effectiveness_rating=7)
    with pytest.raises(NotFoundError):
        svc.create_control(bu.id, "x", risk_ids=["missing"])


def test_event_validation(setup):
    svc, _, risk, control = setup
    with pytest.raises(ValidationError):
        svc.create_risk_event(risk.id, "e", "24-11-2026")
    with pytest.raises(ValidationError):
        svc.create_risk_event(risk.id, "e", "2026-11-24", financial_impact=-5)
    with pytest.raises(ValidationError):
        svc.create_control_event(control.id, "2026-11-24", status="MAYBE")
    with pytest.raises(NotFoundError):
        svc.create_risk_event("missing", "e", "2026-11-24")


def test_deleting_risk_removes_events_and_unlinks_controls(setup):
    svc, _, risk, control = setup
    svc.create_risk_event(risk.id, "e", "2026-11-24")
    svc.delete_risk(risk.id)
    assert svc.repo.list(RiskEvent) == []
    assert svc.get_control(control.id).risk_ids == []


def test_deleting_control_removes_its_tests(setup):
    svc, _, _, control = setup
    svc.create_control_event(control.id, "2026-11-24", status="PASSED")
    svc.delete_control(control.id)
    assert svc.repo.list(ControlEvent) == []


def test_deleting_unit_removes_everything_below(setup):
    svc, bu, _, control = setup
    svc.create_control_event(control.id, "2026-11-24")
    svc.delete_business_unit(bu.id)
    for kind in ("risks", "controls", "risk_events", "control_events"):
        assert svc.export_data()[kind] == [], kind


def test_audit_trail_records_changes_and_actor(setup):
    svc, _, risk, _ = setup
    svc.actor = "alice"
    svc.update_risk(risk.id, category="it")
    latest = svc.list_audit(limit=1)[0]
    assert (latest.action, latest.entity_type, latest.entity_id, latest.user_id) == ("UPDATE", "risk", risk.id, "alice")
    assert latest.changes == {"category": [None, "it"]}
    assert {a.action for a in svc.repo.list(AuditEntry)} >= {"CREATE", "UPDATE"}


def test_timestamps_are_set_and_created_at_kept(setup):
    svc, _, risk, _ = setup
    created = risk.created_at
    assert created and risk.updated_at
    assert svc.update_risk(risk.id, description="x").created_at == created


def test_scores_are_derived_from_distributions(svc):
    bu = svc.create_business_unit("A")
    # P(>=1 event) = 1 - e^-2 = 0.86 -> likelihood 5; median loss 2M -> impact 4
    r = svc.create_risk(bu.id, "big", {"type": "poisson", "rate": 2}, {"dist": "fixed", "value": 2_000_000},
                        residual_frequency={"type": "bernoulli", "p": 0.01})
    s = svc.risk_scores(r)
    assert (s["inherent"]["likelihood"], s["inherent"]["impact"], s["inherent"]["score"]) == (5, 4, 20)
    assert s["inherent"]["level"] == "Very High"
    assert (s["residual"]["likelihood"], s["residual"]["score"]) == (1, 4)
    assert s["inherent"]["expected_annual_loss"] == pytest.approx(4_000_000)


def test_control_effectiveness_uses_latest_test(setup):
    svc, bu, _, control = setup
    other = svc.create_control(bu.id, "Backup")
    svc.create_control_event(control.id, "2026-01-01", status="FAILED")
    svc.create_control_event(control.id, "2026-06-01", status="PASSED")
    svc.create_control_event(other.id, "2026-06-01", status="PARTIAL")
    svc.create_control_event(other.id, "2026-07-01", status="IN_PROGRESS")  # ignored
    assert svc.control_effectiveness() == pytest.approx(75)


def test_dashboard_has_trend_and_severity(svc):
    seed_demo(svc)
    d = svc.dashboard_summary()
    assert len(d["risk_trend"]) == 6
    assert d["risk_trend"][-1]["total"] == 7  # all demo risks were created now
    assert sum(x["count"] for x in d["risks_by_severity"]) == 7
    assert d["counts"]["controls"] == 9 and d["counts"]["controls_implemented"] == 4
    assert 0 <= d["control_effectiveness"] <= 100
    assert d["top_units_by_risk_count"][0]["count"] == 2  # IT


def test_unit_risk_summary(svc):
    seed_demo(svc)
    it = next(u for u in svc.unit_risk_summary() if u["name"] == "IT")
    assert it["risk_count"] == 2 and it["code"] == "IT"
    assert it["average_residual_score"] <= it["average_inherent_score"]


def test_import_merge_with_references_to_existing(svc):
    bu = svc.create_business_unit("Existing")
    data = {"risks": [{"id": "r1", "name": "new", "business_unit_id": bu.id,
                       "frequency": FREQ, "impact": IMPACT}]}
    assert svc.import_data(data, replace=False)["risk"] == 1
    assert svc.get_risk("r1").business_unit_id == bu.id


def test_import_rejects_duplicate_unit_names(svc):
    data = {"business_units": [{"id": "a", "name": "X"}, {"id": "b", "name": "X"}]}
    with pytest.raises(Exception):
        svc.import_data(data)
