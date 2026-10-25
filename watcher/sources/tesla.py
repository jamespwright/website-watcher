"""Tesla used inventory, fetched through a real Chrome.

How it works (investigated Oct 2026):
  * tesla.com sits behind Akamai Bot Manager. The page and
        GET /inventory/api/v4/inventory-results?query=<url-encoded JSON>
    return 403 (or a 429 `cpr_chlge` challenge) to python-requests and to curl_cffi with any
    impersonation, even when given the cookies of a browser session that passed. Headless Chrome
    gets "Access Denied" on the page itself.
  * A normal (headed) Chrome works: once Akamai's script has run on an inventory page, the page's
    own same-origin fetch of the API succeeds. So this source starts Chrome with `nodriver` (an
    automation library that doesn't leave the usual webdriver traces), opens the inventory page
    and runs the API fetch inside it. On Linux without a display (e.g. Docker) it starts Xvfb, a
    virtual screen, for Chrome to draw on. A run takes about 10-30 seconds.
  * If Chrome isn't available or the fetch fails, the source falls back to
    `data/tesla-snapshot.json` (captured by hand with `watcher/browser/tesla_capture.js`) and
    warns when it is older than `max_age_hours` (default 24).
  * Prices are per registration state (`CashDetailsByRegion`); the API rejects a
    `RegistrationProvince` option (412). `source_options.tesla.RegistrationProvince` picks which
    price to use (default: the profile's `search.state`).
  * `state` is the vehicle's location, which can differ from the registration state.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from watcher.core.models import FetchContext, Listing, SourceBlocked, SourceUnavailable
from watcher.core.source import Source, register_source

MODEL_CODES = {"model 3": "m3", "model y": "my", "model s": "ms", "model x": "mx"}
MODEL_NAMES = {v: k.title() for k, v in MODEL_CODES.items()}
MODEL_CODES_REV = {name: code for code, name in MODEL_NAMES.items()}
DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "tesla-snapshot.json"
PAGE_SIZE = 50
MAX_PAGES = 10
TIMEOUT = 150  # seconds for the whole browser session
ATTEMPTS = 4  # API fetches tried per page while Akamai's script settles

# One Chrome at a time: runs of several profiles would otherwise start several browsers.
_browser_lock = threading.Lock()

# Runs in the page; resolves to {status, body}.
FETCH_JS = """fetch("/inventory/api/v4/inventory-results?query=" + encodeURIComponent(%s),
      {credentials: "include", headers: {Accept: "application/json"}})
  .then(async r => JSON.stringify({status: r.status, body: await r.text()}))"""


@register_source
class Tesla(Source):
    name = "tesla"
    label = "Tesla (used inventory)"
    domain = "cars"
    base_url = "https://www.tesla.com"
    notes = ("Akamai blocks scripts and headless browsers; fetched from inside a real Chrome page "
             "(nodriver, Xvfb when there's no display). Falls back to data/tesla-snapshot.json.")

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        return [f"{self.base_url}/en_AU/inventory/used/{self._model_code(search)}"]

    @staticmethod
    def _model_code(search: dict[str, Any]) -> str:
        return MODEL_CODES.get(str(search.get("model", "")).casefold(), "my")

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        code = self._model_code(search)
        try:
            vehicles = self._fetch_live(search, code, ctx)
        except Exception as e:  # noqa: BLE001 - any browser failure falls back to the snapshot
            path = Path(self.options.get("snapshot", DEFAULT_SNAPSHOT))
            if not path.exists():
                raise
            ctx.warn(f"live fetch failed ({e}); using {path.name}")
            vehicles, code = self._read_snapshot(path, ctx)

        reg_state = self.options.get("RegistrationProvince") or search.get("state")
        model = MODEL_NAMES.get(code, "Model Y")
        return [self._to_listing(v, model, reg_state, ctx) for v in vehicles]

    # --- live -----------------------------------------------------------------------------------

    def _query(self, search: dict[str, Any], code: str, offset: int) -> dict[str, Any]:
        trim = self.options.get("TRIM")
        options = {"TRIM": trim if isinstance(trim, list) else [trim]} if trim else {}
        return {
            "query": {
                "model": code, "condition": "used", "options": options, "arrangeby": "Price",
                "order": "asc", "market": "AU", "language": "en", "super_region": "north america",
                "PaymentType": "cash", "paymentRange": {}, "zip": str(search.get("postcode") or ""),
                "range": 0,
            },
            "offset": offset, "count": PAGE_SIZE, "outsideOffset": 0, "outsideSearch": False,
        }

    def _fetch_live(self, search: dict[str, Any], code: str, ctx: FetchContext) -> list[dict[str, Any]]:
        try:
            import nodriver  # noqa: F401 - optional dependency; checked before starting anything
        except ImportError as e:
            raise SourceUnavailable("nodriver is not installed (pip install nodriver)") from e
        with _browser_lock, _display(), tempfile.TemporaryDirectory(prefix="tesla-chrome-") as profile:
            results = asyncio.run(asyncio.wait_for(self._browse(search, code, profile, ctx), TIMEOUT))
        return [_vehicle(r) for r in results]

    async def _browse(self, search: dict[str, Any], code: str, profile: str,
                      ctx: FetchContext) -> list[dict[str, Any]]:
        import nodriver as uc

        try:
            browser = await uc.start(headless=False, sandbox=False, user_data_dir=profile,
                                     browser_args=["--lang=en-AU", "--disable-dev-shm-usage"])
        except Exception as e:  # noqa: BLE001 - e.g. Chrome not installed
            raise SourceUnavailable(f"couldn't start Chrome: {e}") from e
        try:
            tab = await browser.get(self.search_urls(search)[0])
            if "access denied" in str(await tab.evaluate("document.title")).lower():
                raise SourceBlocked("HTTP 403 - Access Denied (Akamai) on the inventory page")
            results: list[dict[str, Any]] = []
            for page in range(MAX_PAGES):
                data = await self._api(tab, self._query(search, code, page * PAGE_SIZE))
                batch = data.get("results") or []
                # The API returns a dict instead of a list when nothing matches.
                results += batch if isinstance(batch, list) else []
                total = int(data.get("total_matches_found") or 0)
                if len(results) >= total or len(batch) < PAGE_SIZE:
                    return results
            ctx.warn(f"only the first {MAX_PAGES} pages read")
            return results
        finally:
            browser.stop()
            # stop() doesn't wait; Chrome must be gone before its profile folder is deleted.
            if proc := getattr(browser, "_process", None):
                try:
                    await asyncio.wait_for(proc.wait(), 15)
                except asyncio.TimeoutError:
                    proc.kill()

    @staticmethod
    async def _api(tab: Any, query: dict[str, Any]) -> dict[str, Any]:
        js = FETCH_JS % json.dumps(json.dumps(query))
        status, body = 0, ""
        for attempt in range(ATTEMPTS):
            await tab.sleep(3 if attempt == 0 else 5)  # let Akamai's sensor script post first
            resp = json.loads(await tab.evaluate(js, await_promise=True))
            status, body = resp["status"], resp["body"]
            if status == 200:
                return json.loads(body)
            if status not in (403, 429):
                break
        if status in (403, 429):
            raise SourceBlocked(f"HTTP {status} from inventory-results inside Chrome")
        raise SourceUnavailable(f"inventory-results returned HTTP {status}: {body[:200]}")

    # --- snapshot -------------------------------------------------------------------------------

    def _read_snapshot(self, path: Path, ctx: FetchContext) -> tuple[list[dict[str, Any]], str]:
        snap = json.loads(path.read_text(encoding="utf-8"))
        captured = datetime.fromisoformat(snap["captured_at"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds() / 3600
        if age_h > float(self.options.get("max_age_hours", 24)):
            ctx.warn(f"snapshot is {age_h:.0f}h old ({captured:%Y-%m-%d %H:%M}); re-capture for fresh data")
        return snap.get("vehicles", []), snap.get("model", "my")

    # --- mapping --------------------------------------------------------------------------------

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
                "seller_type": "dealer",
            },
            raw=v,
        )


def _vehicle(r: dict[str, Any]) -> dict[str, Any]:
    """An API result in the snapshot's shape (see watcher/browser/tesla_capture.js)."""
    year = str(r.get("Year") or "")[:4]
    return {
        "vin": r["VIN"], "trim": r.get("TrimName"), "year": int(year) if year.isdigit() else None,
        "odometer": r.get("Odometer"), "paint": "/".join(r.get("PAINT") or []), "city": r.get("City"),
        "state": r.get("StateProvince"), "in_transit": r.get("InTransit"),
        "first_registered": str(r.get("FirstRegistrationDate") or "")[:10] or None,
        "prices": {k: (v.get("cash") or {}).get("grossPrice")
                   for k, v in (r.get("CashDetailsByRegion") or {}).items()},
    }


@contextmanager
def _display() -> Iterator[None]:
    """Give Chrome a screen: Akamai blocks headless Chrome, so on a Linux box without one
    (Docker, SSH) run it on a temporary Xvfb display."""
    if not sys.platform.startswith("linux") or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        yield
        return
    if not shutil.which("Xvfb"):
        raise SourceUnavailable("no display and Xvfb isn't installed (apt install xvfb)")
    num = 99 + os.getpid() % 100
    proc = subprocess.Popen(["Xvfb", f":{num}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = f":{num}"
    try:
        for _ in range(50):  # wait for the X socket
            if Path(f"/tmp/.X11-unix/X{num}").exists() or proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is not None:
            raise SourceUnavailable(f"Xvfb exited with code {proc.returncode}")
        yield
    finally:
        os.environ.pop("DISPLAY", None)
        proc.terminate()
        proc.wait(timeout=10)
