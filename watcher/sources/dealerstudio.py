"""DealerStudio platform (powers many Australian dealer sites).

How it works (investigated Oct 2026):
  * Search pages are Next.js (app router) and embed the first ~20 hits in the RSC payload.
  * Further pages are fetched client-side from Meilisearch:
        POST {meilisearch_host}multi-search   (Authorization: Bearer <scoped key>)
        body: {"queries":[{"indexUid":"Car_production","filter":[...],"limit":..,"offset":..}]}
  * The scoped key is a Meilisearch tenant token baked into a JS bundle as
    `scoped_meilisearch_api_key:"..."` next to `meilisearch_host:"..."`. It is restricted to
    the dealer's own stock and is public by design (the browser uses it).

Subclass this with a `base_url` for each DealerStudio-powered dealer.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin

from watcher.core import nextjs
from watcher.core.http import Http
from watcher.core.models import FetchContext, Listing
from watcher.core.source import Source

_KEY_RE = re.compile(r'scoped_meilisearch_api_key\\?"?:\\?"([\w.-]+)')
_HOST_RE = re.compile(r'meilisearch_host\\?"?:\\?"(https://[^"\\]+)')
_SCRIPT_RE = re.compile(r'src="(/_next/static/[^"]+\.js)"')


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class DealerStudioSource(Source):
    base_url: str = ""
    index: str = "Car_production"
    page_size: int = 100
    domain = "cars"

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        parts = [slug(search[k]) for k in ("make", "model") if search.get(k)]
        return [f"{self.base_url}/search/{'/'.join(parts)}"]

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        page_url = self.search_urls(search)[0]
        html = ctx.http.get(page_url).text
        creds = self._find_credentials(html, ctx.http)
        if creds:
            try:
                return [self._to_listing(h) for h in self._query_api(*creds, search, ctx.http)]
            except Exception as e:  # noqa: BLE001 - fall back to the embedded page data
                ctx.warn(f"Meilisearch API failed ({e}); using page data")
        else:
            ctx.warn("search API key not found; using page data (first page only)")
        return [self._to_listing(h) for h in self._page_hits(html, ctx)]

    # --- API path -------------------------------------------------------
    def _find_credentials(self, html: str, http: Http) -> tuple[str, str] | None:
        def scan(text: str) -> tuple[str, str] | None:
            k, h = _KEY_RE.search(text), _HOST_RE.search(text)
            return (h.group(1), k.group(1)) if k and h else None

        if found := scan(html):
            return found
        scripts = [urljoin(self.base_url, s) for s in dict.fromkeys(_SCRIPT_RE.findall(html))]

        def get(url: str) -> str:
            try:
                return http.probe(url).text
            except Exception:  # noqa: BLE001
                return ""

        with ThreadPoolExecutor(max_workers=8) as pool:
            for text in pool.map(get, scripts):
                if found := scan(text):
                    return found
        return None

    def _query_api(self, host: str, key: str, search: dict[str, Any], http: Http) -> list[dict[str, Any]]:
        filters = [f'{f}="{search[f]}"' for f in ("make", "model") if search.get(f)]
        hits: list[dict[str, Any]] = []
        offset = 0
        while True:
            body = {"queries": [{"indexUid": self.index, "q": "", "filter": filters,
                                 "limit": self.page_size, "offset": offset}]}
            res = http.post_json(urljoin(host, "multi-search"), body,
                                 headers={"Authorization": f"Bearer {key}"})["results"][0]
            batch = res.get("hits", [])
            hits += batch
            offset += len(batch)
            if not batch or offset >= res.get("estimatedTotalHits", 0):
                return hits

    # --- page fallback --------------------------------------------------
    def _page_hits(self, html: str, ctx: FetchContext) -> list[dict[str, Any]]:
        containers = nextjs.find_objects(html, lambda d: isinstance(d.get("hits"), list) and "count" in d)
        if not containers:
            return []
        c = max(containers, key=lambda d: len(d["hits"]))
        if c.get("count", 0) > len(c["hits"]):
            ctx.warn(f"page shows {len(c['hits'])} of {c['count']} results")
        return c["hits"]

    def image(self, raw: dict[str, Any]) -> str | None:
        url = raw.get("primary_image_url")
        return None if not url or url.endswith("/default.jpg") else url  # default.jpg is a "no photo" card

    def _to_listing(self, h: dict[str, Any]) -> Listing:
        status = "reserved" if h.get("reserved") and h.get("status") == "in_stock" else h.get("status")
        return Listing(
            source=self.name,
            id=str(h.get("id")),
            url=urljoin(self.base_url, h.get("path") or f"/cars/{h.get('slug', '')}"),
            title=h.get("name") or " ".join(str(h.get(k, "")) for k in ("year", "make", "model", "badge")),
            fields={
                "make": h.get("make"), "model": h.get("model"), "badge": h.get("badge"),
                "year": h.get("year"), "price": h.get("price"), "odometer": h.get("km"),
                "colour": h.get("colour"), "body": h.get("simple_body"), "drive": h.get("simple_drive"),
                "state": h.get("state"), "location": h.get("city") or h.get("location_name"),
                "status": status, "vin": h.get("vin"), "seller": h.get("dealership_name"),
            },
            raw=h,
        )
