"""EV Depot (evdepot.au), a used-EV dealer in Seaford VIC.

How it works (investigated Oct 2026):
  * /inventory is a Next.js (app router) page rendered on the server. `?q=<text>` narrows it
    (e.g. "tesla model y") and `?page=<n>` pages through it, 12 cars per page. The cards are
    rendered markup with no data objects, so we only take the /inventory/<stock>-<slug> links.
  * Each car's page embeds a schema.org `Car` as JSON-LD: brand, model, vehicleConfiguration
    (the badge, upper case), modelDate, VIN, color, mileageFromOdometer and offers.price /
    availability. That is where the fields come from.
  * Single dealership, so every car is in VIC and sold by a dealer.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlencode

from watcher.core.http import Http
from watcher.core.models import FetchContext, Listing, SourceBlocked
from watcher.core.source import Source, register_source

BASE_URL = "https://www.evdepot.au"
MAX_PAGES = 10
_LINK_RE = re.compile(r'href="/inventory/(\d+-[a-z0-9-]+)"')
_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
STATUS = {"InStock": "available", "LimitedAvailability": "available", "PreOrder": "reserved",
          "SoldOut": "sold", "OutOfStock": "sold"}


def _car(html: str) -> dict[str, Any] | None:
    for block in _LD_RE.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Car":
            return data
    return None


@register_source
class EvDepot(Source):
    name = "evdepot"
    label = "EV Depot"
    domain = "cars"
    base_url = BASE_URL
    notes = "Server-rendered /inventory?q= links, then schema.org Car JSON-LD from each car's page."

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        q = " ".join(str(search[k]) for k in ("make", "model") if search.get(k))
        return [f"{BASE_URL}/inventory" + (f"?{urlencode({'q': q})}" if q else "")]

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        url = self.search_urls(search)[0]
        slugs: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            page_url = url if page == 1 else f"{url}{'&' if '?' in url else '?'}page={page}"
            new = [s for s in dict.fromkeys(_LINK_RE.findall(ctx.http.get(page_url).text)) if s not in slugs]
            if not new:
                break
            slugs += new
        else:
            ctx.incomplete(f"stopped after {MAX_PAGES} pages")
        with ThreadPoolExecutor(max_workers=6) as pool:
            cars = list(pool.map(lambda s: self._details(ctx.http, s), slugs))
        listings = [self._to_listing(slug, car) for slug, car in zip(slugs, cars) if car]
        if slugs and not listings:
            raise SourceBlocked("evdepot car pages had no Car JSON-LD (layout change?)")
        for slug, car in zip(slugs, cars):
            if not car:
                ctx.incomplete(f"no Car data on /inventory/{slug}")
        return listings

    @staticmethod
    def _details(http: Http, slug: str) -> dict[str, Any] | None:
        try:
            return _car(http.get(f"{BASE_URL}/inventory/{slug}").text)
        except Exception:  # noqa: BLE001 - one bad page shouldn't sink the rest
            return None

    def image(self, raw: dict[str, Any]) -> str | None:
        images = raw.get("image") or []
        return images[0] if isinstance(images, list) and images else images or None

    def _to_listing(self, slug: str, car: dict[str, Any]) -> Listing:
        offer = car.get("offers") or {}
        badge = car.get("vehicleConfiguration")
        return Listing(
            source=self.name,
            id=slug.split("-", 1)[0],  # the stock number
            url=f"{BASE_URL}/inventory/{slug}",
            title=car.get("name") or slug,
            fields={
                "make": (car.get("brand") or {}).get("name"), "model": car.get("model"),
                "badge": badge.title() if badge else None, "year": car.get("modelDate"),
                "price": offer.get("price"), "odometer": (car.get("mileageFromOdometer") or {}).get("value"),
                "colour": car.get("color"), "fuel": car.get("fuelType"), "body": car.get("bodyType"),
                "vin": car.get("vehicleIdentificationNumber"), "state": "VIC", "location": "Seaford",
                "status": STATUS.get(str(offer.get("availability", "")).rsplit("/", 1)[-1]),
                "seller": (offer.get("seller") or {}).get("name") or "EV Depot",
                "seller_type": "dealer",  # EV Depot sells its own stock
            },
            raw=car,
        )
