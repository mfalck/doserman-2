"""The Repository contract: every backend must pass these."""

from riskmesh.domain.models import Asset, BusinessUnit, Person, Risk
from riskmesh.storage import JsonRepository


def test_save_get_roundtrip_keeps_all_fields(repo):
    risk = Risk(name="R", business_unit_id="bu1",
                frequency={"type": "poisson", "rate": 0.5},
                impact={"dist": "pert", "min": 1, "mode": 2, "max": 3},
                asset_ids=["a1", "a2"], residual_impact={"dist": "fixed", "value": 1})
    repo.save(risk)
    assert repo.get(Risk, risk.id) == risk


def test_empty_lists_and_none_survive(repo):
    bu = BusinessUnit(name="X")
    repo.save(bu)
    got = repo.get(BusinessUnit, bu.id)
    assert got.delegate_ids == [] and got.parent_id is None


def test_get_missing_returns_none(repo):
    assert repo.get(Person, "nope") is None


def test_save_is_upsert(repo):
    p = Person(name="A")
    repo.save(p)
    p.name = "B"
    repo.save(p)
    assert [x.name for x in repo.list(Person)] == ["B"]


def test_list_filters(repo):
    repo.save(Asset(name="a", asset_type="t", business_unit_id="u1"))
    repo.save(Asset(name="b", asset_type="t", business_unit_id="u2"))
    root = BusinessUnit(name="root")
    repo.save(root)
    repo.save(BusinessUnit(name="child", parent_id=root.id))
    assert [a.name for a in repo.list(Asset, business_unit_id="u2")] == ["b"]
    assert [b.name for b in repo.list(BusinessUnit, parent_id=None)] == ["root"]


def test_delete_and_clear(repo):
    p = Person(name="A")
    repo.save(p)
    assert repo.delete(Person, p.id) is True
    assert repo.delete(Person, p.id) is False
    repo.save(Person(name="B"))
    repo.clear()
    assert repo.list(Person) == []


def test_json_file_persists_between_instances(tmp_path):
    path = str(tmp_path / "d.json")
    p = Person(name="Persisted")
    JsonRepository(path).save(p)
    assert JsonRepository(path).get(Person, p.id) == p
