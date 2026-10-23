"""Probe arbitrary URLs and report how (and whether) their data can be scraped.

Looks for: HTTP status / bot walls, framework hints (Next.js RSC, __NEXT_DATA__, JSON-LD),
embedded objects that look like listings, and API endpoints / search backends referenced
by the page (and, with deep=True, by its same-origin JavaScript bundles).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urljoin

from rich.console import Console
from rich.table import Table

from . import nextjs
from .http import Http, detect_block

SEARCH_BACKENDS = {
    "meilisearch": re.compile(r"meilisearch", re.I),
    "algolia": re.compile(r"algolia(net)?\.(com|net)|x-algolia", re.I),
    "typesense": re.compile(r"typesense", re.I),
    "elasticsearch": re.compile(r"elasticsearch|opensearch", re.I),
    "graphql": re.compile(r"/graphql\b", re.I),
}
API_URL_RE = re.compile(r"""https?://[a-z0-9.-]*(?:api|search|listing|inventory|gateway|bff)[a-z0-9.-]*\.[a-z]{2,}[^\s"'`<>\\]{0,80}""", re.I)
API_PATH_RE = re.compile(r"""["'`](/api/[a-zA-Z0-9_\-/{}$.]+|/[a-z-]+/v\d+/[a-zA-Z0-9_\-/]+)""")
SCRIPT_RE = re.compile(r"""<script[^>]+src=["']([^"']+\.js[^"']*)["']""", re.I)
JSONLD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


@dataclass
class Finding:
    url: str
    status: int | None = None
    size: int = 0
    content_type: str = ""
    blocked: str | None = None
    framework: list[str] = field(default_factory=list)
    item_objects: int = 0
    sample_keys: list[str] = field(default_factory=list)
    backends: set[str] = field(default_factory=set)
    api_urls: set[str] = field(default_factory=set)
    api_paths: set[str] = field(default_factory=set)
    scripts_scanned: int = 0
    error: str | None = None


def _scan_text(text: str, f: Finding) -> None:
    for name, rx in SEARCH_BACKENDS.items():
        if rx.search(text):
            f.backends.add(name)
    f.api_urls.update(u.rstrip(".,;)") for u in API_URL_RE.findall(text))
    f.api_paths.update(API_PATH_RE.findall(text))


def investigate_url(url: str, http: Http, item_keys: list[str], deep: bool = False) -> Finding:
    f = Finding(url)
    try:
        resp = http.probe(url)
    except Exception as e:  # noqa: BLE001
        f.error = f"{type(e).__name__}: {e}"
        return f
    html = resp.text
    f.status, f.size, f.content_type = resp.status_code, len(resp.content), resp.headers.get("content-type", "")
    f.blocked = detect_block(resp.status_code, html)
    if f.blocked:
        return f

    if "self.__next_f" in html:
        f.framework.append("Next.js app router (RSC)")
    if "__NEXT_DATA__" in html:
        f.framework.append("Next.js pages router (__NEXT_DATA__)")
    if "__NUXT__" in html:
        f.framework.append("Nuxt")
    jsonld = JSONLD_RE.findall(html)
    if jsonld:
        f.framework.append(f"JSON-LD x{len(jsonld)}")

    # Embedded objects that carry all the "item" keys (e.g. price + year) look like listings.
    keys = [k.casefold() for k in item_keys]
    has_keys = lambda d: all(any(k == dk.casefold() for dk in d) for k in keys)  # noqa: E731
    objs = nextjs.find_objects(html, has_keys)
    if not objs and jsonld:
        for block in jsonld:
            try:
                objs += [d for d in nextjs.iter_dicts(json.loads(block)) if has_keys(d)]
            except ValueError:
                pass
    f.item_objects = len(objs)
    if objs:
        f.sample_keys = sorted(objs[0])[:40]

    _scan_text(html, f)
    if deep:
        scripts = {urljoin(url, s) for s in SCRIPT_RE.findall(html)}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for text in pool.map(lambda s: _safe_get(http, s), scripts):
                if text:
                    f.scripts_scanned += 1
                    _scan_text(text, f)
    f.api_urls = {u for u in f.api_urls if not u.endswith((".png", ".svg", ".jpg", ".webp", ".css"))}
    return f


def _safe_get(http: Http, url: str) -> str | None:
    try:
        r = http.probe(url)
        return r.text if r.ok else None
    except Exception:  # noqa: BLE001
        return None


def render(findings: list[Finding], console: Console, notes: dict[str, str] | None = None) -> None:
    t = Table(title="Investigation summary", title_justify="left")
    for col in ("url", "status", "size", "blocked", "framework", "items"):
        t.add_column(col, overflow="fold")
    for f in findings:
        t.add_row(f.url, str(f.status or f.error), f"{f.size:,}", f.blocked or "-",
                  ", ".join(f.framework) or "-", str(f.item_objects))
    console.print(t)
    for f in findings:
        console.rule(f.url[:100], style="dim")
        if notes and f.url in notes and notes[f.url]:
            console.print(f"[cyan]Known:[/] {notes[f.url]}")
        if f.error or f.blocked:
            console.print(f"[red]{f.error or f.blocked}[/] - plain HTTP cannot read this page; a real browser would be needed.")
            continue
        if f.sample_keys:
            console.print(f"[green]Embedded item object keys:[/] {', '.join(f.sample_keys)}")
        if f.backends:
            console.print(f"[green]Search backend hints:[/] {', '.join(sorted(f.backends))}")
        if f.scripts_scanned:
            console.print(f"[dim]Scanned {f.scripts_scanned} JS bundles[/]")
        for label, items in (("API hosts/URLs", f.api_urls), ("API paths", f.api_paths)):
            if items:
                shown = sorted(items)[:25]
                console.print(f"[green]{label}[/] ({len(items)}):")
                for u in shown:
                    console.print(f"  {u}")
