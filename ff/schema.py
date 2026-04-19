"""Schema primitives for declarative EA configs.

Every knob in every EA is one of three things, possibly nested:

- **Leaf** — a single terminal knob (FloatRange / IntRange / Choice).
- **Group** — an on/off block whose sub-knobs exist only when the switch is on.
- **Branch** — an N-way exclusive choice with per-arm sub-knobs.

A schema is a plain dict whose leaves are one of these types. Composition is
unconstrained: Groups can nest Branches, Branches can nest Groups, Leaves are
terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator


# ── Leaves ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FloatRange:
    """A floating-point knob with min, max, scale, and optional step.

    ``scale`` is ``"linear"`` or ``"log"``. When ``step`` is given, ``expand()``
    returns a discretised grid; otherwise the range is continuous and the sampler
    draws directly from it. Indicator-grid leaves MUST have ``step`` set (the
    signal library requires a finite enumerable set); engine knobs may leave it
    ``None``.
    """
    min: float
    max: float
    scale: str = "linear"   # "linear" | "log"
    step: float | None = None

    def __post_init__(self) -> None:
        if self.max <= self.min:
            raise ValueError(f"FloatRange: max ({self.max}) must be > min ({self.min})")
        if self.scale not in ("linear", "log"):
            raise ValueError(f"FloatRange: scale must be 'linear' or 'log', got {self.scale!r}")
        if self.scale == "log" and self.min <= 0:
            raise ValueError(f"FloatRange: log scale requires min > 0, got {self.min}")
        if self.step is not None and self.step <= 0:
            raise ValueError(f"FloatRange: step must be > 0, got {self.step}")


@dataclass(frozen=True)
class IntRange:
    """An integer knob with min, max (inclusive) and step.

    ``step`` defaults to 1. Always discretised (step is mandatory for ints).
    """
    min: int
    max: int
    step: int = 1

    def __post_init__(self) -> None:
        if self.max < self.min:
            raise ValueError(f"IntRange: max ({self.max}) must be >= min ({self.min})")
        if self.step < 1:
            raise ValueError(f"IntRange: step must be >= 1, got {self.step}")


@dataclass(frozen=True)
class Choice:
    """A categorical knob — a fixed list of values. Values can be any hashable."""
    values: tuple

    def __init__(self, values: Iterable) -> None:
        vs = tuple(values)
        if len(vs) == 0:
            raise ValueError("Choice: need at least one value")
        object.__setattr__(self, "values", vs)


Leaf = FloatRange | IntRange | Choice


# ── Composite nodes ────────────────────────────────────────────────────

@dataclass
class Group:
    """An on/off block.

    ``test`` is a Choice of two values (typically ``[True, False]``) — the
    switch. ``when_on`` is the dict of sub-knobs that exist when the switch is
    the "on" value. The off value produces no sub-knobs at all.

    ``on_value`` names which member of ``test.values`` counts as "on". Default
    ``True``. If the switch is numeric, set ``on_value`` to the integer that
    means on (e.g. 1).
    """
    test: Choice
    when_on: dict[str, Any]
    on_value: Any = True

    def __post_init__(self) -> None:
        if self.on_value not in self.test.values:
            raise ValueError(
                f"Group: on_value {self.on_value!r} not in test.values {self.test.values!r}"
            )
        if len(self.test.values) < 2:
            raise ValueError("Group: test needs at least two values")


@dataclass
class Branch:
    """An N-way exclusive choice with per-arm sub-knobs.

    ``selector`` is a Choice over arm names. ``arms`` maps each arm name to a
    dict of sub-knobs that apply only when that arm is chosen. Not every arm
    needs sub-knobs — an empty dict is fine (useful when an arm is a pure "mode"
    like ``"off"`` that flips the engine slot but carries no data).
    """
    selector: Choice
    arms: dict[str, dict[str, Any]]

    def __post_init__(self) -> None:
        missing = set(self.selector.values) - set(self.arms)
        if missing:
            raise ValueError(f"Branch: arms dict missing entries for selector values {missing!r}")
        extra = set(self.arms) - set(self.selector.values)
        if extra:
            raise ValueError(f"Branch: arms dict has keys not in selector.values: {extra!r}")


Node = Leaf | Group | Branch


# ── Helpers ────────────────────────────────────────────────────────────

def expand(leaf: Leaf) -> list:
    """Enumerate the concrete values of a Leaf.

    For ``FloatRange`` with no step → raises ValueError (caller must sample).
    For ``IntRange`` / stepped ``FloatRange`` / ``Choice`` → returns the full list.
    """
    if isinstance(leaf, Choice):
        return list(leaf.values)
    if isinstance(leaf, IntRange):
        return list(range(leaf.min, leaf.max + 1, leaf.step))
    if isinstance(leaf, FloatRange):
        if leaf.step is None:
            raise ValueError("expand(): FloatRange without step is continuous — cannot enumerate")
        # Honest simplest interpretation: grid uses a literal additive step.
        # ``scale`` affects optimiser sampling density (in sampler.py), not the
        # enumeration grid produced here.
        vs: list[float] = []
        v = leaf.min
        while v <= leaf.max + 1e-12:
            vs.append(v)
            v += leaf.step
        return vs
    raise TypeError(f"expand(): unsupported leaf type {type(leaf).__name__}")


def walk(schema: dict, prefix: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    """Yield (path, node) for every node in the schema tree.

    Path is a tuple of strings representing the nested key chain. ``node`` is a
    Leaf, Group, or Branch. Group and Branch sub-trees are recursed into.
    """
    for k, v in schema.items():
        path = prefix + (k,)
        if isinstance(v, dict):
            yield from walk(v, path)
        else:
            yield path, v
            if isinstance(v, Group):
                yield from walk(v.when_on, path + ("when_on",))
            elif isinstance(v, Branch):
                for arm_name, arm_subtree in v.arms.items():
                    yield from walk(arm_subtree, path + ("when", arm_name))


def count_leaves(schema: dict) -> int:
    """Count the number of terminal Leaf nodes (ignores Groups/Branches themselves)."""
    return sum(1 for _, n in walk(schema) if isinstance(n, (FloatRange, IntRange, Choice)))


def validate(schema: dict) -> list[str]:
    """Return a list of human-readable problems. Empty list = OK.

    Checks for duplicate paths, zero-cardinality nodes, and obvious structural issues.
    Does NOT check engine-mapping consistency — that's encoding.py's job.
    """
    problems: list[str] = []
    seen: set[tuple] = set()
    for path, node in walk(schema):
        if path in seen:
            problems.append(f"duplicate path: {'.'.join(path)}")
        seen.add(path)
        if isinstance(node, Choice) and len(node.values) == 0:
            problems.append(f"{'.'.join(path)}: Choice with no values")
        if isinstance(node, IntRange) and node.min == node.max:
            problems.append(f"{'.'.join(path)}: IntRange with a single value — use Choice instead")
    return problems


# ── Self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    sample = {
        "signals": {
            "ema_cross": {
                "fast": IntRange(5, 100, step=5),
                "slow": IntRange(10, 500, step=10),
            },
        },
        "engine": {
            "stop_loss": Branch(
                selector=Choice(["fixed_pips", "atr"]),
                arms={
                    "fixed_pips": {"pips": FloatRange(5, 500, scale="log", step=5)},
                    "atr": {"mult": FloatRange(0.3, 6.0, scale="linear", step=0.1)},
                },
            ),
            "trailing": Group(
                test=Choice([True, False]),
                on_value=True,
                when_on={
                    "activate": FloatRange(5, 100, scale="log", step=5),
                    "mode": Branch(
                        selector=Choice(["fixed", "atr"]),
                        arms={
                            "fixed": {"distance": FloatRange(5, 50, scale="log", step=5)},
                            "atr":   {"mult": FloatRange(0.3, 4.0, step=0.1)},
                        },
                    ),
                },
            ),
        },
    }
    problems = validate(sample)
    print(f"validate: {len(problems)} problems" + (" OK" if not problems else ""))
    for p in problems:
        print("  -", p)
    print(f"leaf count: {count_leaves(sample)}")
    fast_grid = expand(sample["signals"]["ema_cross"]["fast"])
    print(f"ema_cross.fast grid: {fast_grid[:5]}... ({len(fast_grid)} values)")
    print("schema.py: OK")
