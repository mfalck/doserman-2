"""Business rules, run against every backend."""

import pytest

from riskmesh.domain.errors import ConflictError, NotFoundError, ValidationError
from riskmesh.services.demo import seed_demo

FREQ = {"type": "poisson", "rate": 1.0}
IMPACT = {"dist": "fixed", "value": 1000}


def test_unit_names_are_unique(svc):
    svc.create_business_unit("A")
    with pytest.raises(ConflictError):
        svc.create_business_unit("A")


def test_unknown_parent_is_rejected(svc):
    with pytest.raises(NotFoundError):
        svc.create_business_unit("A", parent_id="missing")


def test_cannot_move_unit_below_itself(svc):
    a = svc.create_business_unit("A")
    b = svc.create_business_unit("B", parent_id=a.id)
    with pytest.raises(ValidationError):
        svc.update_business_unit(a.id, parent_id=b.id)


def test_delete_unit_with_children_needs_cascade(svc):
    a = svc.create_business_unit("A")
    b = svc.create_business_unit("B", parent_id=a.id)
    asset = svc.create_asset(b.id, "srv", "virtual")
    svc.create_risk(b.id, "r", FREQ, IMPACT, asset_ids=[asset.id])
    with pytest.raises(ConflictError):
        svc.delete_business_unit(a.id)
    assert set(svc.delete_business_unit(a.id, cascade=True)) == {a.id, b.id}
    assert svc.list_assets() == [] and svc.list_risks() == []


def test_deleting_asset_unlinks_it_from_risks(svc):
    bu = svc.create_business_unit("A")
    asset = svc.create_asset(bu.id, "srv", "virtual")
    risk = svc.create_risk(bu.id, "r", FREQ, IMPACT, asset_ids=[asset.id])
    svc.delete_asset(asset.id)
    assert svc.get_risk(risk.id).asset_ids == []


def test_deleting_person_clears_roles(svc):
    bu = svc.create_business_unit("A")
    p = svc.create_person("P")
    svc.assign_risk_owner(bu.id, p.id)
    svc.add_delegate(bu.id, p.id)
    risk = svc.create_risk(bu.id, "r", FREQ, IMPACT, owner_id=p.id)
    svc.delete_person(p.id)
    bu = svc.get_business_unit(bu.id)
    assert bu.risk_owner_id is None and bu.delegate_ids == []
    assert svc.get_risk(risk.id).owner_id is None


def test_delegates_are_not_duplicated(svc):
    bu = svc.create_business_unit("A")
    p = svc.create_person("P")
    svc.add_delegate(bu.id, p.id)
    svc.add_delegate(bu.id, p.id)
    assert svc.get_business_unit(bu.id).delegate_ids == [p.id]
    svc.remove_delegate(bu.id, p.id)
    assert svc.get_business_unit(bu.id).delegate_ids == []


@pytest.mark.parametrize("freq, impact", [
    ({"type": "poisson", "rate": -1}, IMPACT),
    ({"type": "weekly"}, IMPACT),
    (FREQ, {"dist": "lognorm_p10_p90", "p10": 10, "p90": 5}),
    (FREQ, {"dist": "no-such-dist"}),
])
def test_invalid_distributions_are_rejected(svc, freq, impact):
    bu = svc.create_business_unit("A")
    with pytest.raises(ValidationError):
        svc.create_risk(bu.id, "r", freq, impact)


def test_risk_references_are_checked(svc):
    bu = svc.create_business_unit("A")
    with pytest.raises(NotFoundError):
        svc.create_risk(bu.id, "r", FREQ, IMPACT, asset_ids=["missing"])
    with pytest.raises(NotFoundError):
        svc.create_risk("missing", "r", FREQ, IMPACT)


def test_simulation_rolls_up_and_is_reproducible(svc):
    seed_demo(svc)
    a = svc.simulation_report(n_trials=2_000, seed=1)
    b = svc.simulation_report(n_trials=2_000, seed=1)
    assert a["total"] == b["total"]
    hq = next(u for u in a["business_units"] if u["name"] == "Headquarters")
    assert hq["rolled_up"]["mean"] == pytest.approx(a["total"]["mean"])


def test_controls_reduce_loss(svc):
    seed_demo(svc)
    cmp = svc.compare_inherent_residual(n_trials=5_000, seed=3)
    assert cmp["reduction_in_expected_loss"] > 0


def test_export_import_roundtrip_preserves_ids_and_results(svc):
    seed_demo(svc)
    data = svc.export_data()
    before = svc.simulation_report(n_trials=1_000, seed=9)["total"]
    svc.repo.clear()
    counts = svc.import_data(data)
    assert counts == {"person": 5, "business_unit": 7, "asset": 4, "risk": 7,
                      "control": 5, "risk_event": 3, "control_event": 4}
    assert svc.simulation_report(n_trials=1_000, seed=9)["total"] == before


def test_import_with_broken_reference_writes_nothing(svc):
    seed_demo(svc)
    data = svc.export_data()
    data["risks"][0]["business_unit_id"] = "missing"
    with pytest.raises(ValidationError):
        svc.import_data(data)
    assert len(svc.list_risks()) == 7  # untouched


def test_dashboard_summary(svc):
    seed_demo(svc)
    d = svc.dashboard_summary()
    assert d["counts"]["risks"] == 7
    assert d["counts"]["risks_with_controls"] == 3
    assert sum(d["risks_by_category"].values()) == 7
    assert d["top_risks"][0]["expected_annual_loss"] >= d["top_risks"][-1]["expected_annual_loss"]
