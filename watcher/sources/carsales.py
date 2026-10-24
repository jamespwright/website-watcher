"""carsales.com.au.

How it works (investigated Oct 2026):
  * Search URLs use a query DSL in `q`, e.g.
        (And.(C.Make.Tesla._.(C.Model.Model Y._.(Or.Badge.Long Range._.Badge.Long Range Launch Series.)))_.State.Victoria.)
  * The page is a server-driven-UI Next.js (pages router) app. __NEXT_DATA__ holds `ListingCard`
    objects whose text lives in nested `Text` nodes: `title` (year make model + badge/variant),
    `keyDetails` (body, fuel, odometer next to a `kms-taco` icon), `price` and `sellerTitle`
    ("Dealer used · VIC", "Private · VIC", or "New car to order"). `action.data.prefetchSellerType`
    says "dealer" or "private" and becomes `seller_type`.
  * Plain HTTP is challenged intermittently (a ~775 byte captcha page or a 403), so we retry a
    few times with a fresh session. Detail pages are captcha'd far more often and the cards have
    no colour, but the query DSL has a `Colour.<name>.` filter. So colour is recovered by re-running
    the search once per base colour (And-ed onto the query) and tagging the listing ids returned.
    Disable with source_options: {carsales: {colours: false}} or pass a list to limit the colours.
  * "New car to order" cards have no odometer and are marked status `new` (not `available`).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

from watcher.core import nextjs
from watcher.core.http import Http

from watcher.core.models import FetchContext, Listing, SourceBlocked
from watcher.core.source import Source, register_source
from watcher.domains.cars import _COLOURS, state_name


def build_query(search: dict[str, Any]) -> str:
    inner = None
    badges = search.get("badges") or []
    if badges:
        inner = "(Or." + "._.".join(f"Badge.{b}" for b in badges) + ".)" if len(badges) > 1 else f"Badge.{badges[0]}."
    if search.get("model"):
        inner = f"(C.Model.{search['model']}" + (f"._.{inner})" if inner else ".)")
    if search.get("make"):
        inner = f"(C.Make.{search['make']}" + (f"._.{inner})" if inner else ".)")
    clauses = [c for c in (inner, f"State.{state_name(search['state'])}." if search.get("state") else None) if c]
    if not clauses:
        return ""
    return clauses[0] if len(clauses) == 1 else "(And." + "_.".join(clauses) + ")"


BASE_URL = "https://www.carsales.com.au"
ATTEMPTS = 5
_TITLE_RE = re.compile(r"^(?P<year>\d{4})\s+(?P<make>\S+)\s+(?P<rest>.+)$")


def _texts(node: Any) -> list[str]:
    """Text node values in document order (nextjs.iter_dicts walks in reverse)."""
    if isinstance(node, dict):
        own = [node["value"].strip()] if node.get("type") == "Text" and node.get("value") else []
        return own + [t for v in node.values() for t in _texts(v)]
    if isinstance(node, list):
        return [t for v in node for t in _texts(v)]
    return []


def _seller(card: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(seller type, state, status) from the sellerTitle line."""
    text = next((t for t in _texts(card.get("sellerTitle")) if "verif" not in t and "guarantee" not in t), "")
    if "to order" in text.lower():
        return "Dealer", None, "new"
    seller = re.split(r"\s*[^\w\s]\s*", text, maxsplit=1)[0]
    m = re.search(r"\b(VIC|NSW|QLD|SA|WA|TAS|ACT|NT)\b", text)
    return seller or None, m.group(1) if m else None, "available"


@register_source
class Carsales(Source):
    name = "carsales"
    label = "carsales"
    domain = "cars"
    base_url = BASE_URL
    notes = "__NEXT_DATA__ ListingCards; intermittent captcha so requests are retried. No colour."

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        return [f"{BASE_URL}/cars/?q={quote(build_query(search), safe='().')}"]

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        html = self._get(self.search_urls(search)[0], ctx)
        data = nextjs.next_data(html)
        cards = {c["id"]: c for c in nextjs.iter_dicts(data) if c.get("type") == "ListingCard" and "sellerTitle" in c}
        if not cards:
            raise SourceBlocked("carsales page had no listings (captcha or layout change)")
        listings = [l for c in cards.values() if (l := self._to_listing(c))]
        self._add_colours(listings, search, ctx)
        if len(cards) >= 30:
            ctx.warn(f"{len(cards)} cards on page 1 - results beyond the first page are not read")
        return listings

    def _add_colours(self, listings: list[Listing], search: dict[str, Any], ctx: FetchContext) -> None:
        opt = self.options.get("colours", True)
        if opt is False:
            return
        colours = opt if isinstance(opt, list) else [name for name, _ in _COLOURS]
        base = build_query(search)
        by_id = {l.id: l for l in listings}

        def ids_for(colour: str) -> tuple[str, set[str]]:
            q = f"(And.{base}_.Colour.{colour}.)" if base else f"Colour.{colour}."
            html = self._get(f"{BASE_URL}/cars/?q={quote(q, safe='().')}", ctx)
            cards = nextjs.iter_dicts(nextjs.next_data(html))
            return colour, {c["id"] for c in cards if c.get("type") == "ListingCard" and "sellerTitle" in c}

        def safe(colour: str) -> tuple[str, set[str]]:
            try:
                return ids_for(colour)
            except SourceBlocked as e:
                ctx.warn(f"carsales colour {colour} skipped: {e.reason}")
                return colour, set()

        with ThreadPoolExecutor(max_workers=3) as pool:
            for colour, ids in pool.map(safe, colours):
                for i in ids & by_id.keys():
                    by_id[i].fields["colour"] = colour

    @staticmethod
    def _get(url: str, ctx: FetchContext) -> str:
        """GET with a fresh session per attempt; the captcha is intermittent."""
        error: SourceBlocked | None = None
        for attempt in range(ATTEMPTS):
            try:
                return Http().get(url).text
            except SourceBlocked as e:
                error = e
                ctx.warn(f"carsales blocked attempt {attempt + 1}/{ATTEMPTS}: {e.reason}")
        raise error or SourceBlocked("carsales blocked")

    def image(self, raw: dict[str, Any]) -> str | None:
        slides = ((raw.get("gallery") or {}).get("children")) or []
        return next((s["url"] for s in slides if s.get("type") == "Image" and s.get("url")), None)

    def _to_listing(self, card: dict[str, Any]) -> Listing | None:
        data = (card.get("action") or {}).get("data") or {}
        if not data.get("url"):
            return None
        title = " ".join(_texts(card.get("title"))[:2])
        m = _TITLE_RE.match(title)
        if not m:
            return None
        make, rest = m["make"], m["rest"]
        # "Model Y Long Range Auto AWD": the model is not separable from the badge in text, so use the
        # tracking metadata (which has clean make/model/year/price) to split it.
        meta = next((d for d in nextjs.iter_dicts(card.get("action")) if "model" in d and "price" in d), {})
        model = meta.get("model") or rest.split()[0]
        badge = rest[len(model):].strip() if rest.startswith(model) else rest
        detail = _texts(card.get("keyDetails"))
        odo = next((t for t in detail if re.fullmatch(r"[\d,]+\s*km", t)), None)
        fuel = next((t for t in detail if t in ("Electric", "Petrol", "Diesel", "Hybrid", "Plug-in Hybrid")), None)
        body = detail[0] if detail else None
        price = (_texts(card.get("price")) or [meta.get("price")])[0]
        seller, state, status = _seller(card)
        return Listing(
            source=self.name,
            id=str(card["id"]),
            url=BASE_URL + data["url"],
            title=title,
            fields={
                "make": make, "model": model, "badge": badge, "year": m["year"], "price": price,
                "odometer": odo, "fuel": fuel, "body": body, "state": state, "status": status,
                "seller": seller,
                # "dealer" / "private"; carsales mixes both
                "seller_type": data.get("prefetchSellerType"),
            },
            raw=card,
        )
