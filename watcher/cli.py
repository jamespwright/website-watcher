"""Command line entry point: `python -m watcher <command>`."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from watcher.core import investigate as inv
from watcher.core.http import Http
from watcher.core.output import render, to_json
from watcher.core.profile import load_profile
from watcher.core.runner import run
from watcher.core.source import sources
from watcher.core.store import DEFAULT_DB, Store

PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    report = run(load_profile(args.profile))
    if not args.no_save:
        store = Store(args.db)
        try:
            store.save(report)
        finally:
            store.close()
    if args.json:
        print(to_json(report))
    else:
        render(report, console, verbose=args.verbose)
    return 0


def cmd_db(args: argparse.Namespace, console: Console) -> int:
    if not Path(args.db).exists():
        console.print(f"[red]No database at {args.db} - do a `watcher run` first")
        return 2
    store = Store(args.db)
    try:
        report = store.report(load_profile(args.profile))
    finally:
        store.close()
    if args.json:
        print(to_json(report))
    else:
        render(report, console, verbose=args.verbose)
    return 0


def cmd_web(args: argparse.Namespace, console: Console) -> int:
    from watcher.web.server import serve

    profiles = [Path(p) for p in args.profiles] or sorted(PROFILES.glob("*.yaml"))
    if not profiles:
        console.print(f"[red]No profiles given and none found in {PROFILES}")
        return 2
    serve(profiles, Path(args.db), args.host, args.port, say=console.print)
    return 0


def cmd_investigate(args: argparse.Namespace, console: Console) -> int:
    urls: list[str] = list(args.urls)
    notes: dict[str, str] = {}
    if args.profile:
        p = load_profile(args.profile)
        for name in p.sources:
            src = sources.get(name)(p.source_options.get(name))
            for u in src.search_urls(p.search):
                urls.append(u)
                notes[u] = f"[{src.name}] {src.notes}"
    if not urls:
        console.print("[red]Give URLs and/or --profile")
        return 2
    http = Http()
    with console.status(f"Probing {len(urls)} URL(s)..."):
        findings = [inv.investigate_url(u, http, args.item_keys, deep=args.deep) for u in urls]
    inv.render(findings, console, notes)
    return 0


def cmd_sources(args: argparse.Namespace, console: Console) -> int:
    t = Table(title="Registered sources", title_justify="left")
    for col in ("name", "label", "domain", "notes"):
        t.add_column(col, overflow="fold")
    for name, cls in sources.all().items():
        t.add_row(name, cls.label, cls.domain, cls.notes)
    console.print(t)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watcher", description="Search websites for items matching a profile.")
    ap.add_argument("--debug", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a search profile and print matches")
    r.add_argument("profile", help="path to profile YAML")
    r.add_argument("-v", "--verbose", action="store_true", help="also list rejected items and why")
    r.add_argument("--json", action="store_true", help="print JSON instead of tables")
    r.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file to record results in (default: {DEFAULT_DB})")
    r.add_argument("--no-save", action="store_true", help="don't record this run in the database")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("db", help="apply a profile's rules to everything stored in the database (no scraping)")
    d.add_argument("profile", help="path to profile YAML")
    d.add_argument("-v", "--verbose", action="store_true", help="also list rejected items and why")
    d.add_argument("--json", action="store_true", help="print JSON instead of tables")
    d.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file to read (default: {DEFAULT_DB})")
    d.set_defaults(func=cmd_db)

    w = sub.add_parser("web", help="browse the database in a web page (works on phones on your network)")
    w.add_argument("profiles", nargs="*", help=f"profile YAMLs to serve (default: every one in {PROFILES})")
    w.add_argument("--host", default="0.0.0.0",
                   help="address to listen on (default 0.0.0.0 = your whole network; 127.0.0.1 = this PC only)")
    w.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    w.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file to read and record runs in (default: {DEFAULT_DB})")
    w.set_defaults(func=cmd_web)

    i = sub.add_parser("investigate", help="probe URLs for bot walls, embedded data and APIs")
    i.add_argument("urls", nargs="*", help="URLs to probe")
    i.add_argument("--profile", help="also probe every source's search URL from this profile")
    i.add_argument("--deep", action="store_true", help="also scan the page's JS bundles for API endpoints")
    i.add_argument("--item-keys", nargs="+", default=["price", "year"],
                   help="keys an embedded object must have to count as an item (default: price year)")
    i.set_defaults(func=cmd_investigate)

    s = sub.add_parser("sources", help="list available sources")
    s.set_defaults(func=cmd_sources)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    return args.func(args, Console())


if __name__ == "__main__":
    sys.exit(main())
