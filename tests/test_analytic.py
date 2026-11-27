"""The analytic (FFT) engine: exact cases and agreement with Monte Carlo."""

import numpy as np
import pytest
from scipy import stats

from riskmesh.domain import analytic, risk_model
from riskmesh.services.demo import seed_demo


def test_poisson_with_fixed_loss_is_exactly_poisson():
    dist = analytic.aggregate([({"type": "poisson", "rate": 2.0}, {"dist": "fixed", "value": 1000})], 2 ** 12)
    k = np.round(dist.x / 1000).astype(int)
    for n in range(8):
        mass = dist.pmf[k == n].sum()
        assert mass == pytest.approx(stats.poisson.pmf(n, 2.0), abs=1e-9)


def test_bernoulli_has_point_mass_at_zero():
    dist = analytic.aggregate([({"type": "bernoulli", "p": 0.1}, {"dist": "uniform", "min": 1000, "max": 2000})])
    s = dist.statistics()
    assert s["prob_any_loss"] == pytest.approx(0.1)
    assert s["mean"] == pytest.approx(0.1 * 1500, rel=1e-3)
    assert s["p50"] == 0 and 1000 <= s["p95"] <= 2000


@pytest.mark.parametrize("freq, impact", [
    ({"type": "poisson", "rate": 0.5}, {"dist": "lognorm_p10_p90", "p10": 1e4, "p90": 1e6}),
    ({"type": "poisson", "rate": 3.0}, {"dist": "pert", "min": 5e3, "mode": 2.5e4, "max": 2.5e5}),
    ({"type": "bernoulli", "p": 0.2}, {"dist": "triangular", "min": 1e3, "mode": 5e3, "max": 1e4}),
    ({"type": "poisson", "rate": 0.3}, {"dist": "gamma", "params": {"a": 2, "scale": 1000}}),
])
def test_mean_and_variance_match_exact_moments(freq, impact):
    s = analytic.aggregate([(freq, impact)], analytic.RISK_POINTS).statistics()
    d = risk_model.build_impact_distribution(impact)
    if freq["type"] == "poisson":
        mean, var = freq["rate"] * d.mean(), freq["rate"] * d.moment(2)
    else:
        p = freq["p"]
        mean, var = p * d.mean(), p * d.moment(2) - (p * d.mean()) ** 2
    assert s["mean"] == pytest.approx(mean, rel=2e-3)
    # The std of very heavy tails is driven by losses rarer than 1 in a million, which
    # the engine collapses onto that quantile, so it is up to a few percent too low.
    assert s["std"] == pytest.approx(var ** 0.5, rel=5e-2)


def test_sum_of_independent_risks_adds_means_and_variances():
    a = ({"type": "poisson", "rate": 1.0}, {"dist": "pert", "min": 0, "mode": 100, "max": 500})
    b = ({"type": "bernoulli", "p": 0.3}, {"dist": "uniform", "min": 1000, "max": 3000})
    sa, sb = (analytic.aggregate([x]).statistics() for x in (a, b))
    sab = analytic.aggregate([a, b]).statistics()
    assert sab["mean"] == pytest.approx(sa["mean"] + sb["mean"], rel=1e-3)
    assert sab["std"] ** 2 == pytest.approx(sa["std"] ** 2 + sb["std"] ** 2, rel=1e-2)


def test_tvar_is_at_least_var():
    s = analytic.aggregate([({"type": "poisson", "rate": 0.5},
                             {"dist": "lognorm_p10_p90", "p10": 1e4, "p90": 1e6})]).statistics()
    assert s["tvar99"] >= s["p99"] >= s["p90"] >= s["p50"]


def test_agrees_with_monte_carlo_on_demo(svc):
    seed_demo(svc)
    correlated = svc.simulation_report(n_trials=200_000, seed=3, engine="monte_carlo")
    for c in svc.list_correlations():  # the analytic engine adds up independent risks
        svc.delete_correlation(c.id)
    a = svc.simulation_report(engine="analytic")
    m = svc.simulation_report(n_trials=200_000, seed=3, engine="monte_carlo")
    assert correlated["total"]["p99"] > m["total"]["p99"]
    assert correlated["total"]["mean"] == pytest.approx(m["total"]["mean"])
    assert a["engine"] == "analytic" and m["engine"] == "monte_carlo"
    for key, tol in (("mean", 0.02), ("p50", 0.03), ("p90", 0.03), ("p99", 0.06)):
        assert a["total"][key] == pytest.approx(m["total"][key], rel=tol), key
    hq = next(u for u in a["business_units"] if u["name"] == "Headquarters")
    assert hq["rolled_up"]["p99"] == pytest.approx(a["total"]["p99"])


def test_analytic_is_deterministic_and_compares(svc):
    seed_demo(svc)
    assert svc.simulation_report()["total"] == svc.simulation_report()["total"]
    cmp = svc.compare_inherent_residual(engine="analytic")
    assert cmp["reduction_in_expected_loss"] > 0 and cmp["reduction_in_p99"] > 0
