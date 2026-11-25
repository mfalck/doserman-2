"""The quantitative engine on its own."""

import numpy as np
import pytest

from riskmesh.domain import risk_model as m


@pytest.mark.parametrize("freq, impact", [
    ({"type": "poisson", "rate": 0.5}, {"dist": "lognorm_p10_p90", "p10": 1e4, "p90": 1e6}),
    ({"type": "poisson", "rate": 2.0}, {"dist": "pert", "min": 5e3, "mode": 2.5e4, "max": 2.5e5}),
    ({"type": "bernoulli", "p": 0.2}, {"dist": "triangular", "min": 1, "mode": 5, "max": 10}),
    ({"type": "bernoulli", "p": 0.3}, {"dist": "gamma", "params": {"a": 2, "scale": 1000}}),
])
def test_simulated_mean_matches_analytic(freq, impact):
    losses = m.simulate_annual_loss(freq, impact, 200_000, np.random.default_rng(0))
    assert losses.mean() == pytest.approx(m.expected_annual_loss(freq, impact), rel=0.03)


def test_lognormal_from_p10_p90_hits_the_quantiles():
    d = m.build_impact_distribution({"dist": "lognorm_p10_p90", "p10": 100, "p90": 10_000})
    assert d.ppf(0.1) == pytest.approx(100) and d.ppf(0.9) == pytest.approx(10_000)


def test_bernoulli_has_at_most_one_event():
    counts = m.sample_frequency({"type": "bernoulli", "p": 0.5}, 10_000, np.random.default_rng(0))
    assert set(np.unique(counts)) <= {0, 1}


def test_tvar_is_at_least_var():
    losses = np.random.default_rng(0).lognormal(10, 1, 50_000)
    s = m.loss_statistics(losses)
    assert s["tvar99"] >= s["p99"] >= s["p90"] >= s["p50"]


def test_summarize_distribution_returns_plain_floats():
    out = m.summarize_distribution("norm", loc=0, scale=1)
    assert all(isinstance(out[k], float) for k in ("p10", "p50", "p90", "mean", "std", "skew", "kurtosis"))
    assert out["p90"] == pytest.approx(1.2816, abs=1e-3)
