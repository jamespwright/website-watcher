"""Car listings: normalise states, statuses and numbers so criteria work across sites.

Normalised fields (sources should fill whatever they can):
    make, model, badge, year, price, odometer, colour, body, drive,
    state (VIC/NSW/...), location, status (available/reserved/sold), vin, seller,
    seller_type (dealer/private; leave it out when the site doesn't say)
"""
from __future__ import annotations

import re
from typing import Any

from watcher.core.domain import Domain, register_domain

STATES = {
    "VIC": "Victoria", "NSW": "New South Wales", "QLD": "Queensland", "SA": "South Australia",
    "WA": "Western Australia", "TAS": "Tasmania", "ACT": "Australian Capital Territory",
    "NT": "Northern Territory",
}
_STATE_LOOKUP = {**{k.casefold(): k for k in STATES}, **{v.casefold(): k for k, v in STATES.items()}}

_STATUS = {
    "available": "available", "in_stock": "available", "in stock": "available", "for sale": "available",
    "reserved": "reserved", "on hold": "reserved", "deposit taken": "reserved",
    "sold": "sold",
}


# Marketing colour names ("Pearl White Multi-Coat") -> base colour. First match wins.
_COLOURS = [
    ("White", r"white|pearl"), ("Black", r"black|obsidian"), ("Grey", r"gr[ae]y|graphite|stealth|titanium"),
    ("Silver", r"silver|aluminium|aluminum"), ("Blue", r"blue"), ("Red", r"red"), ("Green", r"green"),
    ("Yellow", r"yellow"), ("Orange", r"orange"), ("Brown", r"brown|bronze"), ("Gold", r"gold|beige"),
    ("Purple", r"purple"),
]


def base_colour(value: Any) -> str | None:
    if not value:
        return None
    return next((name for name, rx in _COLOURS if re.search(rx, str(value), re.IGNORECASE)), None)


def state_code(value: Any) -> str | None:
    return _STATE_LOOKUP.get(str(value).strip().casefold()) if value else None


def state_name(value: Any) -> str | None:
    code = state_code(value)
    return STATES.get(code) if code else None


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d.]", "", str(v))
    return int(float(digits)) if digits else None


@register_domain
class Cars(Domain):
    name = "cars"
    display = ["year", "badge", "price", "odometer", "colour", "location", "state", "status"]
    money_fields = {"price"}
    units = {"odometer": "km"}
    dedupe_keys = ["colour", "year", "odometer"]

    def normalise(self, fields: dict[str, Any]) -> dict[str, Any]:
        for f in ("price", "odometer", "year"):
            if f in fields:
                fields[f] = _int(fields[f])
        if fields.get("state"):
            fields["state"] = state_code(fields["state"]) or fields["state"]
        if fields.get("status"):
            fields["status"] = _STATUS.get(str(fields["status"]).strip().casefold(), str(fields["status"]).lower())
        if isinstance(fields.get("seller_type"), str):
            fields["seller_type"] = fields["seller_type"].strip().lower()
        for f in ("make", "model", "badge", "colour"):
            if isinstance(fields.get(f), str):
                fields[f] = fields[f].strip()
        # Keep the marketing name, but compare on the base colour so "Pearl White" matches White.
        if fields.get("colour") and (base := base_colour(fields["colour"])):
            if base.casefold() != fields["colour"].casefold():
                fields.setdefault("colour_detail", fields["colour"])
            fields["colour"] = base
        return fields
