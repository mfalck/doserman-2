"""Period close, frozen facts, trend and comparison - on every backend."""

import pytest

from riskmesh.domain.errors import ConflictError, ValidationError
from riskmesh.domain.models import PeriodFact
from riskmesh.domain.periods import parse_period
from riskmesh.services.demo import seed_demo


def test_period_keys():
    assert parse_period("2026-q4")[0] == "2026-Q4"
    assert str(parse_period("2024-02")[3]) == "2024-02-29"
    assert parse_period("2026")[1] == "year"
    with pytest.raises(ValidationError):
        parse_period("Q4 2026")


def test_close_freezes_figures(svc):
    seed_demo(svc)
    period = svc.close_period("2026-Q4", note="board")
    assert period.status == "closed" and period.run_id and period.facts_hash
    facts = svc.period_facts("2026-Q4")
    levels = {f.level for f in facts}
    assert levels == {"total", "unit", "category", "risk"}
    total = svc.period_facts("2026-Q4", "total")[0]
    assert total.risk_count == 7 and total.eal_residual == pytest.approx(
        sum(f.eal_residual for f in facts if f.level == "risk"))
    hq = next(f for f in facts if f.level == "unit" and f.name == "Headquarters")
    assert hq.risk_count == 7 and hq.p99 == pytest.approx(total.p99)
    # later changes do not touch the frozen figures
    svc.update_risk(svc.list_risks()[0].id, frequency={"type": "poisson", "rate": 9})
    assert svc.period_facts("2026-Q4", "total")[0].eal_residual == total.eal_residual
    with pytest.raises(ConflictError):
        svc.close_period("2026-Q4")
    assert svc.verify_audit()["ok"]


def test_events_are_counted_in_their_period(svc):
    seed_demo(svc)  # demo events are dated March, August and October 2026
    svc.close_period("2026-Q3")
    total = svc.period_facts("2026-Q3", "total")[0]
    assert total.events == 1 and total.actual_loss == 18_500


def test_edited_facts_are_detected(svc):
    seed_demo(svc)
    svc.close_period("2026-11")
    fact = svc.period_facts("2026-11", "total")[0]
    fact.eal_residual = 1.0
    svc.repo.save(fact)
    errors = svc.verify_audit()["data_errors"]
    assert any("frozen figures of period 2026-11" in e["error"] for e in errors)


def test_compare_bridge_adds_up_and_trend(svc):
    seed_demo(svc)
    svc.close_period("2026-Q3")
    risks = svc.list_risks()
    svc.update_risk(risks[0].id, frequency={"type": "poisson", "rate": 3})          # changed estimate
    svc.update_risk(risks[1].id, residual_frequency={"type": "poisson", "rate": 0.001})  # better controls
    svc.delete_risk(risks[2].id)                                                     # removed
    svc.create_risk(svc.list_business_units()[0].id, "New", {"type": "bernoulli", "p": 0.1},
                    {"dist": "fixed", "value": 500_000})                             # new
    svc.close_period("2026-Q4")
    c = svc.compare_periods("2026-Q3", "2026-Q4")
    b = c["bridge"]
    assert b["start"] + b["new_risks"] + b["removed_risks"] + b["changed_estimates"] + b["controls"] == \
        pytest.approx(b["end"])
    assert b["new_risks"] == pytest.approx(50_000) and b["removed_risks"] < 0 and b["controls"] < 0
    statuses = {r["name"]: r["status"] for r in c["risks"]}
    assert statuses["New"] == "new" and statuses[risks[2].name] == "removed"
    assert [t["period"] for t in svc.trend()] == ["2026-Q3", "2026-Q4"]
    unit = c["units"][0]["id"]
    assert len(svc.trend("risk_count", "unit", unit)) == 2


def test_reopen_and_close_again(svc):
    seed_demo(svc)
    svc.close_period("2026")
    svc.reopen_period("2026")
    assert svc.repo.list(PeriodFact, period_key="2026") == []
    assert svc.close_period("2026").status == "closed"
    assert svc.verify_audit()["ok"]


def test_close_anchors_when_configured(svc, tmp_path):
    from riskmesh.domain import anchoring
    svc.anchor_key_path = str(anchoring.generate_key(tmp_path / "k.pem")[0])
    seed_demo(svc)
    period = svc.close_period("2026-Q4")
    assert period.anchor_id and svc.list_anchors()[0].reason == "period 2026-Q4 closed"
    assert svc.verify_audit()["ok"]
