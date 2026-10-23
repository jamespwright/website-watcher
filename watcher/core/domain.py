"""A domain knows how to normalise listings of one kind of thing (cars, bikes, GPUs...)."""
from __future__ import annotations

from typing import Any

from .registry import Registry


class Domain:
    name: str = ""
    #: Default columns shown in the results table (profile `display` overrides).
    display: list[str] = []
    #: Fields formatted as money / with a unit suffix in output.
    money_fields: set[str] = set()
    units: dict[str, str] = {}
    #: Listings on different sites whose fields all match on these are shown as one item.
    dedupe_keys: list[str] = []

    def normalise(self, fields: dict[str, Any]) -> dict[str, Any]:
        return fields


domains: Registry[Domain] = Registry("domain", "watcher.domains")
register_domain = domains.register
