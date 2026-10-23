"""Terminal rendering for run reports."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from rich.console import Console
from rich.table import Table

from .domain import Domain
from .runner import Report, Result

STATUS_STYLE = {"ok": "green", "blocked": "red", "error": "red", "skipped": "yellow", "unavailable": "yellow"}


def fmt(field: str, value: Any, domain: Domain) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (int, float)) and field != "year":
        text = f"{value:,}"
        if field in domain.money_fields:
            text = f"${text}"
        if field in domain.units:
            text = f"{text} {domain.units[field]}"
        return text
    return str(value)


def _price_cell(m: Result, domain: Domain) -> str:
    text = fmt("price", m.listing.fields.get("price"), domain)
    first, now = m.first_price, m.listing.fields.get("price")
    if first is None or now is None or first == now:
        return text
    colour = "green" if now < first else "red"
    return f"{text} [{colour}](was {fmt('price', first, domain)})[/]"


def render(report: Report, console: Console, verbose: bool = False) -> None:
    p, domain = report.profile, report.domain
    console.rule(f"[bold]{p.name}")

    if report.sources:  # empty when the report comes from the database
        st = Table(title="Sources", title_justify="left", show_lines=False)
        for col in ("source", "status", "fetched", "matched", "notes"):
            st.add_column(col)
        for r in report.sources:
            notes = r.message or "; ".join(r.warnings)
            st.add_row(r.label, f"[{STATUS_STYLE.get(r.status, '')}]{r.status}", str(r.fetched), str(r.matched), notes)
        console.print(st)

    if p.required:
        console.print("[dim]Required:[/] " + "; ".join(c.describe() for c in p.required))
    if p.optional:
        console.print("[dim]Optional:[/] " + "; ".join(
            c.describe() + (f" (x{c.weight:g})" if c.weight != 1 else "") for c in p.optional))

    cols = p.display or domain.display or sorted({k for m in report.matches for k in m.listing.fields})
    stored = any(m.first_seen for m in report.matches)
    new = sum(all(c.is_new for c in m.copies()) for m in report.matches)
    title = f"{len(report.matches)} match(es)" + (f", {new} new" if new else "")
    t = Table(title=title, title_justify="left", show_lines=False)
    t.add_column("#", justify="right")
    if p.optional:
        t.add_column("score", justify="right")
    if stored:
        t.add_column("found")
    t.add_column("source")
    t.add_column("title")
    for c in cols:
        t.add_column(c, justify="right" if c in domain.money_fields or c in domain.units else "left")
    t.add_column("link", overflow="fold")

    for i, m in enumerate(report.matches, 1):
        ev, lst = m.evaluation, m.listing
        row = [str(i)]
        if p.optional:
            row.append(f"{ev.score:g}/{ev.max_score:g}")
        copies = m.copies()
        if stored:
            # A car already known from another site isn't new just because it showed up on this one.
            found = min(c.first_seen or "" for c in copies)[:10]
            row.append(f"[bold green]{found} new[/]" if all(c.is_new for c in copies) else found)
        row += [", ".join(c.listing.source for c in copies), lst.title]
        row += [_price_cell(m, domain) if c == "price" else fmt(c, lst.fields.get(c), domain) for c in cols]
        row.append("\n".join(f"[link={c.listing.url}]{c.listing.url}[/link]" for c in copies))
        t.add_row(*row)
    console.print(t)

    if report.rejected:
        reasons = Counter(r.evaluation.failures[0].split("=")[0].split(" ")[0] for r in report.rejected)
        summary = ", ".join(f"{k}: {v}" for k, v in reasons.most_common())
        console.print(f"[dim]{len(report.rejected)} rejected (first failing field - {summary}). Use -v for detail.")
    if verbose and report.rejected:
        rt = Table(title="Rejected", title_justify="left")
        for col in ("source", "title", "why"):
            rt.add_column(col)
        for r in report.rejected:
            rt.add_row(r.listing.source, r.listing.title, "; ".join(r.evaluation.failures))
        console.print(rt)


def to_json(report: Report) -> str:
    return json.dumps({
        "profile": report.profile.name,
        "sources": [r.__dict__ for r in report.sources],
        "matches": [{
            "source": m.listing.source, "id": m.listing.id, "title": m.listing.title, "url": m.listing.url,
            "score": m.evaluation.score, "max_score": m.evaluation.max_score,
            "matched_optional": m.evaluation.matched, "fields": m.listing.fields,
            "first_seen": m.first_seen, "last_seen": m.last_seen, "first_price": m.first_price,
            "new": m.is_new,
            "also": [{"source": c.listing.source, "id": c.listing.id, "url": c.listing.url,
                      "fields": c.listing.fields} for c in m.also],
        } for m in report.matches],
    }, indent=2, default=str)
