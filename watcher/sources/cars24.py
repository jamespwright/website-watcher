"""Cars24 Australia.

How it works (investigated Oct 2026):
  * Make/model pages like /buy-used-tesla-model-y/ are Next.js (app router). The full result
    set (20 per page) is embedded in the RSC payload as objects with `appointmentId` +
    `listingPrice`, plus a page object {"hasMore", "totalElements", ...}.
  * The page also embeds unrelated "recommended" cars - criteria on make/model filter them.
  * The underlying JSON API (POST https://listing-service-au.c24.tech/listing/v2/buy-used-car)
    returns 401 without browser session headers, so we read the page instead.
  * Listings show a pickup hub (Melbourne/Sydney/Brisbane) rather than a state; we map it.
  * The results page has no colour. Each car's detail page (a separate, older React app)
    embeds `window.__PRELOADED_STATE__` with `carDetails.content` holding color, vin,
    state, suburb, driveType etc. We fetch those for cars matching the searched make/model.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from watcher.core import nextjs
from watcher.core.http import Http
from watcher.core.models import FetchContext, Listing
from watcher.core.source import Source, register_source

HUB_STATE = {"melbourne": "VIC", "sydney": "NSW", "brisbane": "QLD", "adelaide": "SA", "perth": "WA"}


_STATE_RE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _details(http: Http, url: str) -> dict[str, Any] | None:
    """carDetails.content from a car's detail page, or None if unavailable."""
    try:
        html = http.get(url).text
        m = _STATE_RE.search(html)
        state, _ = json.JSONDecoder().raw_decode(html, m.end()) if m else ({}, 0)
        return state.get("carDetails", {}).get("content") or None
    except Exception:  # noqa: BLE001 - details are a bonus; the listing stands without them
        return None


@register_source
class Cars24(Source):
    name = "cars24"
    label = "Cars24"
    domain = "cars"
    base_url = "https://www.cars24.com.au"
    notes = ("Next.js RSC page data (complete for <=20 results). JSON API at listing-service-au.c24.tech "
             "requires browser session auth (401).")

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        parts = "-".join(_slug(search[k]) for k in ("make", "model") if search.get(k))
        return [f"{self.base_url}/buy-used-{parts}/" if parts else f"{self.base_url}/buy-used-cars-australia/"]

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        html = ctx.http.get(self.search_urls(search)[0]).text
        cars = nextjs.find_objects(html, lambda d: "appointmentId" in d and "listingPrice" in d)
        pages = nextjs.find_objects(html, lambda d: "hasMore" in d and "totalElements" in d)
        if pages and pages[0].get("hasMore"):
            ctx.warn(f"only first page read ({pages[0].get('size')} of {pages[0].get('totalElements')})")
        unique = {c["appointmentId"]: c for c in cars}
        listings = [self._to_listing(c) for c in unique.values()]
        if self.options.get("details", True):
            self._add_details([l for l in listings if self._matches_search(l, search)], ctx)
        return listings

    @staticmethod
    def _matches_search(listing: Listing, search: dict[str, Any]) -> bool:
        return all(str(listing.fields.get(k, "")).casefold() == str(search[k]).casefold()
                   for k in ("make", "model") if search.get(k))

    def _add_details(self, listings: list[Listing], ctx: FetchContext) -> None:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for listing, details in zip(listings, pool.map(lambda l: _details(ctx.http, l.url), listings)):
                if details is None:
                    ctx.warn(f"no detail data for {listing.id}")
                    continue
                listing.raw["details"] = details
                listing.fields.update({k: v for k, v in {
                    "colour": details.get("color"),
                    "vin": details.get("vin"),
                    "state": details.get("state"),
                    "location": details.get("suburbCity") or listing.fields.get("location"),
                    "drive": details.get("driveType"),
                    "body": details.get("bodyType"),
                }.items() if v})

    def image(self, raw: dict[str, Any]) -> str | None:
        return (raw.get("listingImage") or {}).get("uri") or None

    def _to_listing(self, c: dict[str, Any]) -> Listing:
        def val(key: str) -> Any:
            v = c.get(key)
            return v.get("value") if isinstance(v, dict) else v

        hub = (c.get("location") or {}).get("hubName") if isinstance(c.get("location"), dict) else None
        return Listing(
            source=self.name,
            id=str(c["appointmentId"]),
            url=c.get("deepLinkUrl") or f"{self.base_url}/{c.get('cdpRelativeUrl', '')}",
            title=f"{c.get('year', '')} {c.get('carName', '')} {c.get('variant', '')}".strip(),
            fields={
                "make": c.get("make"), "model": c.get("model"), "badge": c.get("variant"),
                "year": c.get("year"), "price": val("listingPrice"), "odometer": val("odometer"),
                "colour": c.get("color") or c.get("colour"), "fuel": c.get("fuelType"),
                "state": HUB_STATE.get(str(hub).casefold()) if hub else None, "location": hub,
                "status": c.get("status"), "seller": "Cars24",
            },
            raw=c,
        )
