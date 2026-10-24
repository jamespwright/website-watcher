"""Autotrader Australia (autotrader.com.au).

How it works (investigated Oct 2026):
  * Search pages are path based (/for-sale/<make>/<model>/<state>) and built with Nuxt 3; the
    results are embedded in __NUXT_DATA__ as a call to the public listings API below.
  * The API (https://listings.platform.autotrader.com.au/api/v3/search?make=&model=&state=)
    returns Laravel-style pages: `data` (hits with an Elasticsearch `_source`), `last_page`,
    `total`. `paginate` is the page size and must be <= 50.
  * Each `_source` has make, model, variant, series, manu_year, price.advertised_price,
    odometer, colour_base/colour_body, condition (Used/Demo/New), status (Live),
    location_city/location_state, dealer.trading_name and a relative `url`.
  * Like Drive, python-requests gets a 403 on both the page and the API, so we use
    curl_cffi impersonating Chrome.
"""
from __future__ import annotations

import re
from typing import Any

from curl_cffi import requests as cffi

from watcher.core.http import detect_block
from watcher.core.models import FetchContext, Listing, SourceBlocked
from watcher.core.source import Source, register_source

BASE_URL = "https://www.autotrader.com.au"
API_URL = "https://listings.platform.autotrader.com.au/api/v3/search"
PAGE_SIZE = 50
MAX_PAGES = 10
STATUS = {"live": "available"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@register_source
class Autotrader(Source):
    name = "autotrader"
    label = "Autotrader"
    domain = "cars"
    base_url = BASE_URL
    notes = "Public listings API (listings.platform.autotrader.com.au/api/v3/search); 403s python-requests, so curl_cffi is used."

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        parts = [_slug(search[k]) for k in ("make", "model") if search.get(k)]
        if search.get("state"):
            parts.append(_slug(search["state"]))
        return [f"{BASE_URL}/for-sale/{'/'.join(parts)}"]

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        params = {k: search[k] for k in ("make", "model") if search.get(k)}
        if search.get("state"):
            params["state"] = str(search["state"]).lower()
        session = cffi.Session(impersonate="chrome")
        results, page, pages = [], 1, 1
        while page <= min(pages, MAX_PAGES):
            data = self._page(session, {**params, "paginate": PAGE_SIZE, "page": page})
            results += [hit["_source"] for hit in data.get("data") or [] if hit.get("_source")]
            pages = data.get("last_page") or 1
            page += 1
        if pages > MAX_PAGES:
            ctx.warn(f"only the first {MAX_PAGES} of {pages} pages read")
        unique = {r["id"]: r for r in results}
        return [self._to_listing(r) for r in unique.values()]

    @staticmethod
    def _page(session: cffi.Session, params: dict[str, Any]) -> dict[str, Any]:
        resp = session.get(API_URL, params=params, timeout=25,
                           headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}/"})
        reason = detect_block(resp.status_code, resp.text)
        if reason:
            raise SourceBlocked(f"{reason} at {resp.url}")
        resp.raise_for_status()
        return resp.json()

    def image(self, raw: dict[str, Any]) -> str | None:
        photos = sorted((p for p in raw.get("photos") or [] if p.get("image_path")),
                        key=lambda p: p.get("position") or 0)
        return f"https://images.autotrader.com.au/t_listing_grid_at/{photos[0]['image_path']}" if photos else None

    def _to_listing(self, r: dict[str, Any]) -> Listing:
        vehicle = r.get("vehicle") or {}
        price = r.get("price") or {}
        dealer = r.get("dealer") or {}
        condition = r.get("condition") or ""
        status = STATUS.get(str(r.get("status")).lower(), r.get("status"))
        if status == "available" and condition.lower() == "new":
            status = "new"
        title = " ".join(str(v) for v in (r.get("manu_year"), r.get("make"), r.get("model"),
                                          r.get("variant")) if v)
        return Listing(
            source=self.name,
            id=str(r["id"]),
            url=f"{BASE_URL}/{r['url'].lstrip('/')}" if r.get("url") else f"{BASE_URL}/car/{r['id']}",
            title=title,
            fields={
                "make": r.get("make"), "model": r.get("model"), "badge": r.get("variant"),
                "series": r.get("series"), "year": r.get("manu_year"),
                "price": price.get("advertised_price"), "price_driveaway": price.get("driveaway_price") or None,
                "odometer": r.get("odometer"), "colour": r.get("colour_body") or r.get("colour_base") or None,
                "fuel": vehicle.get("fuel_type"), "body": vehicle.get("body_type_style"),
                "drive": vehicle.get("drive_type"), "state": r.get("location_state"),
                "location": r.get("location_city"), "status": status, "condition": condition or None,
                "seller": dealer.get("trading_name") or ("Private" if r.get("is_private") else None),
                "seller_type": "dealer" if r.get("is_dealer") else "private" if r.get("is_private") else None,
            },
            raw=r,
        )
