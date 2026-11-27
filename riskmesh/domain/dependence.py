"""
Correlation between risks: bad years that tend to come together.

Each risk is first simulated on its own, exactly as without correlation.
The annual losses are then reordered across the trials (Iman & Conover,
1982) so that their ranks follow a Gaussian copula with the given
correlations. Every risk keeps exactly its own loss distribution (and its
expected loss); only which bad years coincide changes, and with it the tail
of the total. Risks that are in no correlation are not touched.

A coefficient is the correlation of the drivers behind the two risks. The
correlation of the losses themselves comes out lower when a risk has many
loss-free years (all those zeros tie), which the report shows as `achieved`.
"""

from typing import Dict, List, Sequence, Tuple

import numpy as np
from scipy import stats

Pair = Tuple[str, str, float]


def correlation_matrix(ids: Sequence[str], pairs: Sequence[Pair]) -> Tuple[np.ndarray, bool]:
    """
    The target matrix for `ids` (identity where nothing is given). Pairwise
    estimates do not always fit together (A~B and B~C strongly, A~C strongly
    negative); then the nearest valid matrix is used and `adjusted` is True.
    """
    pos = {rid: i for i, rid in enumerate(ids)}
    c = np.eye(len(ids))
    for a, b, rho in pairs:
        c[pos[a], pos[b]] = c[pos[b], pos[a]] = rho
    values, vectors = np.linalg.eigh(c)
    if values.min() >= 1e-8:
        return c, False
    fixed = vectors @ np.diag(np.clip(values, 1e-6, None)) @ vectors.T
    d = np.sqrt(np.diag(fixed))
    fixed = fixed / np.outer(d, d)
    np.fill_diagonal(fixed, 1.0)
    return fixed, True


def reorder(columns: Dict[str, np.ndarray], pairs: Sequence[Pair],
            rng: np.random.Generator) -> Tuple[Dict[str, np.ndarray], bool]:
    """
    Permutations (one per correlated risk) that give the annual losses in
    `columns` the rank correlation of `pairs`. Apply a risk's permutation to
    everything simulated for it (total and consequence parts alike).
    """
    ids = sorted({rid for a, b, _ in pairs for rid in (a, b)})
    if not ids:
        return {}, False
    target, adjusted = correlation_matrix(ids, pairs)
    n = len(columns[ids[0]])
    scores = stats.norm.ppf(np.arange(1, n + 1) / (n + 1))
    s = np.column_stack([rng.permutation(scores) for _ in ids])
    e = np.corrcoef(s, rowvar=False) if len(ids) > 1 else np.eye(1)
    t = s @ np.linalg.inv(np.linalg.cholesky(e)).T @ np.linalg.cholesky(target).T
    perms = {}
    for j, rid in enumerate(ids):
        ranks = np.argsort(np.argsort(t[:, j], kind="stable"), kind="stable")
        perms[rid] = np.argsort(columns[rid], kind="stable")[ranks]
    return perms, adjusted


def achieved(columns: Dict[str, np.ndarray], pairs: Sequence[Pair]) -> List[Dict[str, float]]:
    """The correlation of the simulated annual losses for each pair (Pearson)."""
    out = []
    for a, b, rho in pairs:
        x, y = columns[a], columns[b]
        r = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else 0.0
        out.append({"risk_a_id": a, "risk_b_id": b, "coefficient": rho, "achieved": r})
    return out
