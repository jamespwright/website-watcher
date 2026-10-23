"""Tesla used inventory, read from a browser-captured snapshot.

How it works (investigated Oct 2026):
  * tesla.com sits behind Akamai: the page and
        GET /inventory/api/v4/inventory-results?query=<url-encoded JSON>
    return 403 to scripts. In a real Chrome tab the same-origin fetch works, so the data is
    captured through the Claude-in-Chrome extension by running `watcher/browser/tesla_capture.js`
    on a tesla.com tab and saving its output to `data/tesla-snapshot.json`.
  * This source reads that snapshot and warns when it is older than `max_age_hours` (default 24).
  * Prices are per registration state (`prices: {VIC: ...}`); the API rejects a
    `RegistrationProvince` option. `source_options.tesla.RegistrationProvince` picks which price
    to use (default: the profile's `search.state`).
  * `state` is the vehicle's location, which can differ from the registration state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watcher.core.models import FetchContext, Listing, SourceUnavailable
from watcher.core.source import Source, register_source

MODEL_CODES = {"model 3": "m3", "model y": "my", "model s": "ms", "model x": "mx"}
MODEL_NAMES = {v: k.title() for k, v in MODEL_CODES.items()}
DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "tesla-snapshot.json"


@register_source
class Tesla(Source):
    name = "tesla"
    label = "Tesla (used inventory)"
    domain = "cars"
    base_url = "https://www.tesla.com"
    notes = ("Akamai blocks scripts; reads data/tesla-snapshot.json captured via Claude-in-Chrome "
             "(watcher/browser/tesla_capture.js).")

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        code = MODEL_CODES.get(str(search.get("model", "")).casefold(), "my")
        return [f"{self.base_url}/en_AU/inventory/used/{code}"]

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        path = Path(self.options.get("snapshot", DEFAULT_SNAPSHOT))
        if not path.exists():
            raise SourceUnavailable(f"no snapshot at {path}; capture one with Claude-in-Chrome "
                                    "(see watcher/browser/tesla_capture.js)")
        snap = json.loads(path.read_text(encoding="utf-8"))

        captured = datetime.fromisoformat(snap["captured_at"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds() / 3600
        if age_h > float(self.options.get("max_age_hours", 24)):
            ctx.warn(f"snapshot is {age_h:.0f}h old ({captured:%Y-%m-%d %H:%M}); re-capture for fresh data")

        reg_state = self.options.get("RegistrationProvince") or search.get("state")
        model = MODEL_NAMES.get(snap.get("model", ""), "Model Y")
        return [self._to_listing(v, model, reg_state, ctx) for v in snap.get("vehicles", [])]

    def _to_listing(self, v: dict[str, Any], model: str, reg_state: str | None, ctx: FetchContext) -> Listing:
        prices = v.get("prices") or {}
        price = prices.get(reg_state) if reg_state else None
        if price is None and prices:
            price = next(iter(prices.values()))
            ctx.warn(f"{v['vin']}: no {reg_state} price, using first available")
        return Listing(
            source=self.name,
            id=v["vin"],
            url=f"{self.base_url}/en_AU/{MODEL_CODES_REV.get(model, 'my')}/order/{v['vin']}?titleStatus=used",
            title=f"{v.get('year', '')} Tesla {model} {v.get('trim', '')}".strip(),
            fields={
                "make": "Tesla", "model": model, "badge": v.get("trim"), "year": v.get("year"),
                "price": price, "odometer": v.get("odometer"), "colour": v.get("paint"),
                "state": v.get("state"), "location": v.get("city"), "vin": v["vin"],
                "status": "reserved" if v.get("in_transit") else "available", "seller": "Tesla",
            },
            raw=v,
        )


MODEL_CODES_REV = {name: code for code, name in MODEL_NAMES.items()}
