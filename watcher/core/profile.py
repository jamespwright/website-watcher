"""Load a search profile (YAML) describing what to look for and where."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .criteria import Criterion, parse_rules


@dataclass
class Profile:
    name: str
    domain: str
    sources: list[str]
    search: dict[str, Any] = field(default_factory=dict)
    source_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: list[Criterion] = field(default_factory=list)
    optional: list[Criterion] = field(default_factory=list)
    aliases: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    display: list[str] | None = None
    sort: list[str] = field(default_factory=lambda: ["-score"])

    def canonicalise(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Map raw values onto canonical ones using `aliases` (first matching regex wins)."""
        for fname, mapping in self.aliases.items():
            value = fields.get(fname)
            if value is None:
                continue
            for canonical, patterns in mapping.items():
                if any(re.search(p, str(value), re.IGNORECASE) for p in patterns):
                    fields.setdefault(f"{fname}_raw", value)
                    fields[fname] = canonical
                    break
        return fields


def load_profile(path: str | Path) -> Profile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not data.get("sources"):
        raise ValueError(f"{path}: profile needs a non-empty `sources` list")
    return Profile(
        name=data.get("name", Path(path).stem),
        domain=data.get("domain", "generic"),
        sources=list(data["sources"]),
        search=data.get("search") or {},
        source_options=data.get("source_options") or {},
        required=parse_rules(data.get("required")),
        optional=parse_rules(data.get("optional")),
        aliases=data.get("aliases") or {},
        display=data.get("display"),
        sort=data.get("sort") or ["-score"],
    )
