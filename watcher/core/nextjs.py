"""Helpers for pulling data out of Next.js pages.

Next.js "app router" pages stream their data as React Server Component (RSC) rows via
`self.__next_f.push([1, "..."])`. Each row looks like `<hex id>:<json>`, and values may
reference other rows as "$<hex id>". Older "pages router" sites use a __NEXT_DATA__ script.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterator

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)
_ROW_RE = re.compile(r"(?m)^([0-9a-f]+):(?=[\[{\"])")
_REF_RE = re.compile(r"\$([0-9a-f]+)")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def rsc_payload(html: str) -> str:
    return "".join(json.loads(f'"{chunk}"') for chunk in _PUSH_RE.findall(html))


def rsc_rows(html: str) -> dict[str, Any]:
    payload = rsc_payload(html)
    decoder = json.JSONDecoder()
    rows: dict[str, Any] = {}
    for m in _ROW_RE.finditer(payload):
        try:
            rows[m.group(1)], _ = decoder.raw_decode(payload, m.end())
        except ValueError:
            continue
    return rows


def next_data(html: str) -> Any | None:
    m = _NEXT_DATA_RE.search(html)
    return json.loads(m.group(1)) if m else None


def resolve(value: Any, rows: dict[str, Any], _depth: int = 0) -> Any:
    """Replace "$<id>" references with the row they point to (recursively)."""
    if _depth > 40:
        return value
    if isinstance(value, str):
        m = _REF_RE.fullmatch(value)
        if m and m.group(1) in rows:
            return resolve(rows[m.group(1)], rows, _depth + 1)
        return value
    if isinstance(value, dict):
        return {k: resolve(v, rows, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, rows, _depth + 1) for v in value]
    return value


def iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    stack = [value]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def find_objects(html: str, predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """All dicts in the page's RSC rows or __NEXT_DATA__ matching `predicate`, refs resolved."""
    rows = rsc_rows(html)
    roots: Any = list(rows.values())
    nd = next_data(html)
    if nd is not None:
        roots.append(nd)
    return [resolve(d, rows) for d in iter_dicts(roots) if predicate(d)]
