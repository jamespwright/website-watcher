"""Required / optional matching rules.

A rule set is a mapping of field -> spec, where spec is one of:

    make: Tesla                      # equals (case-insensitive)
    badge: [Long Range, Performance] # one of
    price: {lte: 55000}              # operators, all must hold
    odometer: {lte: 40000, weight: 2}  # `weight` only matters for optional rules

Operators: eq, ne, in, not_in, lt, lte, gt, gte, contains, regex, exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def _eq(a: Any, b: Any) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a).strip().casefold() == str(b).strip().casefold()


def _cmp(fn: Callable[[float, float], bool]) -> Callable[[Any, Any], bool]:
    def check(a: Any, b: Any) -> bool:
        na, nb = _num(a), _num(b)
        return na is not None and nb is not None and fn(na, nb)

    return check


def _as_list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


OPS: dict[str, tuple[str, Callable[[Any, Any], bool]]] = {
    "eq": ("=", _eq),
    "ne": ("!=", lambda a, b: not _eq(a, b)),
    "in": ("in", lambda a, b: any(_eq(a, x) for x in _as_list(b))),
    "not_in": ("not in", lambda a, b: not any(_eq(a, x) for x in _as_list(b))),
    "lt": ("<", _cmp(lambda a, b: a < b)),
    "lte": ("<=", _cmp(lambda a, b: a <= b)),
    "gt": (">", _cmp(lambda a, b: a > b)),
    "gte": (">=", _cmp(lambda a, b: a >= b)),
    "contains": ("contains", lambda a, b: any(str(b).casefold() in str(x).casefold() for x in _as_list(a))),
    "regex": ("~", lambda a, b: re.search(str(b), str(a), re.IGNORECASE) is not None),
    "exists": ("exists", lambda a, b: (a is not None) == bool(b)),
}


@dataclass
class Criterion:
    field: str
    ops: list[tuple[str, Any]]
    weight: float = 1.0

    def describe(self) -> str:
        return " and ".join(f"{self.field} {OPS[op][0]} {val}" for op, val in self.ops)

    def check(self, fields: dict[str, Any]) -> tuple[bool, str | None]:
        value = fields.get(self.field)
        if value is None and not any(op == "exists" for op, _ in self.ops):
            return False, f"{self.field} unknown"
        for op, expected in self.ops:
            if not OPS[op][1](value, expected):
                return False, f"{self.field}={value!r} fails {OPS[op][0]} {expected}"
        return True, None


def parse_rules(rules: dict[str, Any] | None) -> list[Criterion]:
    out: list[Criterion] = []
    for name, spec in (rules or {}).items():
        weight = 1.0
        if isinstance(spec, dict):
            spec = dict(spec)
            weight = float(spec.pop("weight", 1.0))
            unknown = set(spec) - set(OPS)
            if unknown:
                raise ValueError(f"Rule '{name}': unknown operator(s) {sorted(unknown)}. Valid: {sorted(OPS)}")
            ops = list(spec.items())
        elif isinstance(spec, (list, tuple)):
            ops = [("in", list(spec))]
        else:
            ops = [("eq", spec)]
        out.append(Criterion(name, ops, weight))
    return out


@dataclass
class Evaluation:
    passed: bool
    failures: list[str] = field(default_factory=list)
    score: float = 0.0
    max_score: float = 0.0
    matched: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)


def evaluate(fields: dict[str, Any], required: list[Criterion], optional: list[Criterion]) -> Evaluation:
    ev = Evaluation(passed=True)
    for c in required:
        ok, reason = c.check(fields)
        if not ok:
            ev.passed = False
            ev.failures.append(reason or c.describe())
    for c in optional:
        ev.max_score += c.weight
        ok, _ = c.check(fields)
        if ok:
            ev.score += c.weight
            ev.matched.append(c.field)
        else:
            ev.missed.append(c.field)
    return ev
