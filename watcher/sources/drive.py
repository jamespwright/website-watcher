"""Drive (drive.com.au) cars for sale.

How it works (investigated Oct 2026):
  * Search URLs are path based: /cars-for-sale/search/<state>/<region>/<make>/<model>/, with
    `all` for any state/region and /page/<n>/ appended for later pages (30 per page). The
    patterns are listed in /cars-for-sale/search/sitemap_index.xml.
  * The page is Next.js (pages router). __NEXT_DATA__ props.pageProps.data.listings holds
    `results` (DealerListing objects with makeName, modelName, badgeDescription, colour,
    odometer, price, region.state, dealer) and `pageInfo` (pageCount, itemCount).
  * Cloudflare fingerprints TLS: python-requests gets a 403 "Just a moment..." page while curl
    gets the real page, so we fetch with curl_cffi impersonating Chrome.
  * Listings are dealer stock only (used, demo and new); listType becomes the status.
"""
from __future__ import annotations

import re
from typing import Any

from curl_cffi import requests as cffi

from watcher.core import nextjs
from watcher.core.http import detect_block
from watcher.core.models import FetchContext, Listing, SourceBlocked
from watcher.core.source import Source, register_source

BASE_URL = "https://www.drive.com.au"
MAX_PAGES = 10
STATUS = {"used": "available", "demo": "available", "new": "new"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@register_source
class Drive(Source):
    name = "drive"
    label = "Drive"
    domain = "cars"
    base_url = BASE_URL
    notes = "__NEXT_DATA__ listings; Cloudflare blocks python-requests by TLS fingerprint, so curl_cffi is used."

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        parts = [_slug(search["state"]) if search.get("state") else "all", "all"]
        parts += [_slug(search[k]) for k in ("make", "model") if search.get(k)]
        return [f"{BASE_URL}/cars-for-sale/search/{'/'.join(parts)}/"]

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        url = self.search_urls(search)[0]
        session = cffi.Session(impersonate="chrome")
        results, page, pages = [], 1, 1
        while page <= min(pages, MAX_PAGES):
            listings = self._page(session, url if page == 1 else f"{url}page/{page}/")
            results += listings.get("results") or []
            pages = (listings.get("pageInfo") or {}).get("pageCount") or 1
            page += 1
        if pages > MAX_PAGES:
            ctx.warn(f"only the first {MAX_PAGES} of {pages} pages read")
        unique = {r["id"]: r for r in results}
        return [self._to_listing(r) for r in unique.values()]

    @staticmethod
    def _page(session: cffi.Session, url: str) -> dict[str, Any]:
        resp = session.get(url, timeout=25)
        reason = detect_block(resp.status_code, resp.text)
        if reason:
            raise SourceBlocked(f"{reason} at {url}")
        resp.raise_for_status()
        data = nextjs.next_data(resp.text) or {}
        listings = ((data.get("props") or {}).get("pageProps") or {}).get("data", {}).get("listings")
        if listings is None:
            raise SourceBlocked(f"drive page had no listings data (layout change?) at {url}")
        return listings

    def image(self, raw: dict[str, Any]) -> str | None:
        src = (raw.get("image") or {}).get("srcUrl")
        # The bare URL is the full-size original; ask the CDN for a card-sized copy.
        return src.replace("/obj/", "/obj/tx_q:70,rs:auto:720:480:1/", 1) if src else None

    def _to_listing(self, r: dict[str, Any]) -> Listing:
        region = r.get("region") or {}
        dealer = r.get("dealer") or {}
        title = " ".join(str(v) for v in (r.get("year"), r.get("makeName"), r.get("modelName"),
                                          r.get("badgeDescription")) if v)
        return Listing(
            source=self.name,
            id=str(r["id"]),
            url=f"{BASE_URL}/cars-for-sale/car/{r['id']}/",
            title=title,
            fields={
                "make": r.get("makeName"), "model": r.get("modelName"),
                "badge": r.get("badgeDescription") or r.get("variant"), "variant": r.get("variant"),
                "year": r.get("year"), "price": r.get("price"), "price_driveaway": r.get("priceDriveAway"),
                "odometer": r.get("odometer"), "colour": r.get("colour"), "fuel": r.get("fuelType"),
                "body": r.get("body"), "state": region.get("state") or dealer.get("state"),
                "location": dealer.get("suburb") or region.get("regionName"),
                "status": STATUS.get(r.get("listType"), r.get("listType")), "seller": dealer.get("name"),
            },
            raw=r,
        )
