"""Shared HTTP client with bot-protection detection."""
from __future__ import annotations

from typing import Any

import requests

from .models import SourceBlocked

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# (marker found in a small response body, description)
BLOCK_MARKERS = [
    ("access denied", "Access Denied (likely Akamai)"),
    ("captcha", "captcha challenge"),
    ("cf-chl", "Cloudflare challenge"),
    ("datadome", "DataDome"),
    ("_incapsula_", "Incapsula"),
    ("px-captcha", "PerimeterX"),
]


def detect_block(status: int, text: str) -> str | None:
    """Return a description if a response looks like a bot wall, else None."""
    lowered = text[:20000].lower()
    marker = next((desc for m, desc in BLOCK_MARKERS if m in lowered), None)
    if status in (401, 403, 429):
        return f"HTTP {status}" + (f" - {marker}" if marker else "")
    # Real pages often mention "captcha" in form scripts; only trust markers on tiny pages.
    if marker and len(text) < 5000:
        return marker
    return None


class Http:
    def __init__(self, timeout: float = 25):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-AU,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        })

    def probe(self, url: str, **kw: Any) -> requests.Response:
        """GET without raising - for investigation."""
        return self.session.get(url, timeout=self.timeout, **kw)

    def get(self, url: str, **kw: Any) -> requests.Response:
        resp = self.probe(url, **kw)
        self._check(resp)
        return resp

    def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None) -> Any:
        resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        self._check(resp)
        return resp.json()

    @staticmethod
    def _check(resp: requests.Response) -> None:
        reason = detect_block(resp.status_code, resp.text)
        if reason:
            raise SourceBlocked(f"{reason} at {resp.url}")
        resp.raise_for_status()
