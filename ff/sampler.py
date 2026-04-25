"""Random sampler over a declarative schema.

Respects the on/off semantics of :class:`~ff.schema.Group` (no sub-knobs when
off) and the exclusive-arm semantics of :class:`~ff.schema.Branch` (only the
selected arm's sub-knobs are sampled). This keeps each trial dict shaped so
that downstream encoders correctly see "dead" slots as absent rather than
zero-valued.

Continuous ``FloatRange`` (``step is None``) is sampled log-uniform when
``scale="log"``, otherwise linear-uniform. Stepped Leaves are sampled uniformly
from their enumerated grid. ``IntRange`` is uniform over its stepped grid.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import schema as sc


class RandomSampler:
    """Random sampler. Deterministic for a given ``seed``.

    Parameters
    ----------
    engine_schema:
        The ``engine_schema`` dict from the EA config. Arbitrary nesting of
        dicts, :class:`~ff.schema.Group`, :class:`~ff.schema.Branch`, and
        Leaves (FloatRange / IntRange / Choice).
    n_variants:
        Number of signal-library variants. Each trial picks one uniformly.
    seed:
        RNG seed. Same seed → identical trials (bit-exact).
    """

    def __init__(self, engine_schema: dict, n_variants: int, seed: int = 42) -> None:
        if n_variants < 1:
            raise ValueError(f"n_variants must be >= 1, got {n_variants}")
        self._schema = engine_schema
        self._n_variants = int(n_variants)
        self._rng = np.random.default_rng(seed)
        self.seed = int(seed)

    def sample(self, n_trials: int) -> list[dict]:
        """Return a list of ``n_trials`` trial dicts."""
        return [self._sample_trial() for _ in range(n_trials)]

    def _sample_trial(self) -> dict:
        return {
            "signal_variant": int(self._rng.integers(0, self._n_variants)),
            "engine": self._sample_subtree(self._schema),
        }

    def _sample_subtree(self, subtree: dict) -> dict:
        out: dict = {}
        for key, node in subtree.items():
            out[key] = self._sample_node(node)
        return out

    def _sample_node(self, node: Any) -> Any:
        if isinstance(node, dict):
            return self._sample_subtree(node)
        if isinstance(node, (sc.FloatRange, sc.IntRange, sc.Choice)):
            return self._sample_leaf(node)
        if isinstance(node, sc.Group):
            return self._sample_group(node)
        if isinstance(node, sc.Branch):
            return self._sample_branch(node)
        raise TypeError(f"unsupported schema node type {type(node).__name__}")

    def _sample_leaf(self, leaf) -> Any:
        if isinstance(leaf, sc.Choice):
            idx = int(self._rng.integers(0, len(leaf.values)))
            return leaf.values[idx]
        if isinstance(leaf, sc.IntRange):
            values = list(range(leaf.min, leaf.max + 1, leaf.step))
            return int(values[int(self._rng.integers(0, len(values)))])
        if isinstance(leaf, sc.FloatRange):
            if leaf.step is None:
                if leaf.scale == "log":
                    return float(np.exp(self._rng.uniform(np.log(leaf.min), np.log(leaf.max))))
                return float(self._rng.uniform(leaf.min, leaf.max))
            values = sc.expand(leaf)
            return float(values[int(self._rng.integers(0, len(values)))])
        raise TypeError(f"unsupported leaf type {type(leaf).__name__}")

    def _sample_group(self, group: sc.Group) -> dict:
        test_val = self._sample_leaf(group.test)
        if test_val == group.on_value:
            return {"test": test_val, "when_on": self._sample_subtree(group.when_on)}
        # OFF: sub-knobs omitted entirely. Two off trials have identical records.
        return {"test": test_val}

    def _sample_branch(self, branch: sc.Branch) -> dict:
        arm = self._sample_leaf(branch.selector)
        out: dict = {"selector": arm}
        arm_subtree = branch.arms.get(arm, {})
        if arm_subtree:
            out[arm] = self._sample_subtree(arm_subtree)
        return out


# ── Self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    schema = {
        "stop_loss": sc.Branch(
            selector=sc.Choice(["fixed", "atr"]),
            arms={
                "fixed": {"pips": sc.FloatRange(5, 500, scale="log", step=None)},
                "atr": {"mult": sc.FloatRange(0.3, 6.0, scale="linear", step=None)},
            },
        ),
        "trailing": sc.Group(
            test=sc.Choice([True, False]),
            on_value=True,
            when_on={
                "activate": sc.FloatRange(5, 100, scale="log"),
                "mode": sc.Branch(
                    selector=sc.Choice(["fixed", "atr"]),
                    arms={
                        "fixed": {"distance": sc.FloatRange(5, 50, scale="log")},
                        "atr": {"mult": sc.FloatRange(0.3, 4.0)},
                    },
                ),
            },
        ),
        "days": sc.Choice([31, 63, 127]),
    }
    sampler = RandomSampler(schema, n_variants=5, seed=42)
    trials = sampler.sample(5)
    for i, t in enumerate(trials):
        print(f"trial {i}: variant={t['signal_variant']}")
        print(f"   stop_loss: {t['engine']['stop_loss']}")
        tr = t["engine"]["trailing"]
        if tr["test"]:
            print(f"   trailing ON: activate={tr['when_on']['activate']:.1f} mode={tr['when_on']['mode']}")
        else:
            print("   trailing OFF (no sub-knobs in trial dict)")
        print(f"   days: {t['engine']['days']}")
    # Reproducibility check.
    again = RandomSampler(schema, n_variants=5, seed=42).sample(5)
    assert again == trials, "sampler not deterministic for fixed seed"
    print("sampler.py: OK (deterministic)")
