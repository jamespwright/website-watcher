"""SQLite history of everything a profile has found, matched or not.

Tables:
    runs           one row per `watcher run`
    run_sources    how each source did on that run
    listings       latest fields per (profile, source, id), plus first_seen / last_seen
    price_history  one row whenever a listing's price is first seen or changes
    notifications  which listings each notify channel has already announced
    favourites     listings whose every change is announced (see notify.notify_favourites)
    favourite_changes  what changed on a favourite, until it has been announced

A stored listing that a source fetched without problems no longer returns has been taken
down, so its status is set to `sold`. A blocked site says nothing about its stock, and neither
does one that may have missed listings (more pages than were read, a car page that failed, the
Tesla snapshot) or that returned nothing at all, so those are left alone. A listing comes back
with its real status if it is relisted.

Listings are keyed by profile name, so editing a profile's `name` starts a fresh history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .criteria import evaluate
from .domain import domains
from .models import Listing
from .profile import Profile
from .runner import Report, Result, dedupe, judge, rank
from .source import sources

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "watcher.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    profile     TEXT NOT NULL,
    started_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_sources (
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    source   TEXT NOT NULL,
    status   TEXT NOT NULL,
    message  TEXT,
    fetched  INTEGER,
    matched  INTEGER,
    warnings TEXT,     -- JSON list
    complete INTEGER,  -- 0 if the source may have missed listings
    PRIMARY KEY (run_id, source)
);
CREATE TABLE IF NOT EXISTS listings (
    profile     TEXT NOT NULL,
    source      TEXT NOT NULL,
    id          TEXT NOT NULL,
    url         TEXT,
    title       TEXT,
    fields      TEXT NOT NULL,  -- JSON, normalised + canonicalised
    raw         TEXT,           -- JSON, as the site gave it
    passed      INTEGER,        -- matched `required` on its last run
    score       REAL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (profile, source, id)
);
CREATE TABLE IF NOT EXISTS price_history (
    profile  TEXT NOT NULL,
    source   TEXT NOT NULL,
    id       TEXT NOT NULL,
    seen_at  TEXT NOT NULL,
    price    REAL,
    PRIMARY KEY (profile, source, id, seen_at)
);
CREATE TABLE IF NOT EXISTS notifications (
    profile  TEXT NOT NULL,
    channel  TEXT NOT NULL,
    source   TEXT NOT NULL,
    id       TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (profile, channel, source, id)
);
CREATE TABLE IF NOT EXISTS favourites (
    profile   TEXT NOT NULL,
    source    TEXT NOT NULL,
    id        TEXT NOT NULL,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (profile, source, id)
);
CREATE TABLE IF NOT EXISTS favourite_changes (
    n        INTEGER PRIMARY KEY,
    profile  TEXT NOT NULL,
    source   TEXT NOT NULL,
    id       TEXT NOT NULL,
    seen_at  TEXT NOT NULL,
    field    TEXT NOT NULL,  -- a listing field, or "photo" / "title" / "url"
    old      TEXT,           -- JSON
    new      TEXT,           -- JSON
    sent     INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dumps(v: Any) -> str:
    return json.dumps(v, default=str, ensure_ascii=False)


def _image(source: str, raw: dict[str, Any]) -> str | None:
    try:
        return sources.get(source)().image(raw)
    except Exception:  # noqa: BLE001 - a missing photo is cosmetic
        return None


def _diff(old: sqlite3.Row, lst: Listing) -> list[tuple[str, Any, Any]]:
    """(field, before, after) for everything that differs between a stored listing and a fresh one,
    including the main photo. `*_raw` fields only mirror a canonicalised value, so they're skipped."""
    out = []
    before, after = json.loads(old["fields"]), json.loads(_dumps(lst.fields))
    photo = (_image(lst.source, json.loads(old["raw"] or "{}")), _image(lst.source, lst.raw))
    if photo[0] != photo[1]:
        out.append(("photo", *photo))
    for name, was, now in (("title", old["title"], lst.title), ("url", old["url"], lst.url)):
        if was != now:
            out.append((name, was, now))
    for f in sorted(before.keys() | after.keys()):
        if not f.endswith("_raw") and before.get(f) != after.get(f):
            out.append((f, before.get(f), after.get(f)))
    return out


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that databases made by older versions lack."""
        have = {row["name"] for row in self.db.execute("PRAGMA table_info(run_sources)")}
        with self.db:
            for col, kind in (("warnings", "TEXT"), ("complete", "INTEGER")):
                if col not in have:
                    self.db.execute(f"ALTER TABLE run_sources ADD COLUMN {col} {kind}")

    def close(self) -> None:
        self.db.close()

    def _first_price(self, profile: str, source: str, id: str) -> Any:
        row = self.db.execute(
            "SELECT price FROM price_history WHERE profile=? AND source=? AND id=? ORDER BY seen_at LIMIT 1",
            (profile, source, id)).fetchone()
        return row["price"] if row else None

    def save(self, report: Report) -> None:
        """Record every listing in the report and fill in its first_seen / last_seen / first_price."""
        now, profile = _now(), report.profile.name
        with self.db:
            run_id = self.db.execute("INSERT INTO runs (profile, started_at) VALUES (?, ?)",
                                     (profile, now)).lastrowid
            self.db.executemany(
                """INSERT INTO run_sources (run_id, source, status, message, fetched, matched, warnings, complete)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_id, s.name, s.status, s.message, s.fetched, s.matched, _dumps(s.warnings), int(s.complete))
                 for s in report.sources])
            for r in [c for m in report.matches for c in m.copies()] + report.rejected:
                lst, ev = r.listing, r.evaluation
                key = (profile, lst.source, lst.id)
                old = self.db.execute("SELECT first_seen, fields, raw, title, url FROM listings WHERE profile=? AND source=? AND id=?",
                                      key).fetchone()
                price = lst.fields.get("price")
                old_price = json.loads(old["fields"]).get("price") if old else None
                self.db.execute(
                    """INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (profile, source, id) DO UPDATE SET
                         url=excluded.url, title=excluded.title, fields=excluded.fields, raw=excluded.raw,
                         passed=excluded.passed, score=excluded.score, last_seen=excluded.last_seen""",
                    (*key, lst.url, lst.title, _dumps(lst.fields), _dumps(lst.raw), int(ev.passed), ev.score,
                     now, now))
                if old and self.is_favourite(*key):
                    self._record_changes(key, now, _diff(old, lst))
                if price is not None and (old is None or price != old_price):
                    self.db.execute("INSERT OR REPLACE INTO price_history VALUES (?, ?, ?, ?, ?)", (*key, now, price))
                r.first_seen = old["first_seen"] if old else now
                r.last_seen = now
                r.is_new = old is None
                r.first_price = self._first_price(*key)
            whole = [s.name for s in report.sources if s.status == "ok" and s.complete and s.fetched]
            self._mark_sold(report.profile, whole, now)

    def _mark_sold(self, profile: Profile, ok_sources: list[str], now: str) -> None:
        """Listings these sources (which returned everything) didn't return have been taken down: assume sold."""
        if not ok_sources:
            return
        rows = self.db.execute(
            f"""SELECT source, id, fields FROM listings WHERE profile=? AND last_seen < ?
                AND source IN ({",".join("?" * len(ok_sources))})""",
            (profile.name, now, *ok_sources)).fetchall()
        for row in rows:
            fields = json.loads(row["fields"])
            if fields.get("status") == "sold":
                continue
            if self.is_favourite(profile.name, row["source"], row["id"]):
                self._record_changes((profile.name, row["source"], row["id"]), now,
                                     [("status", fields.get("status"), "sold")])
            fields["status"] = "sold"
            ev = evaluate(fields, profile.required, profile.optional)
            self.db.execute("UPDATE listings SET fields=?, passed=?, score=? WHERE profile=? AND source=? AND id=?",
                            (_dumps(fields), int(ev.passed), ev.score, profile.name, row["source"], row["id"]))

    def is_favourite(self, profile: str, source: str, id: str) -> bool:
        return self.db.execute("SELECT 1 FROM favourites WHERE profile=? AND source=? AND id=?",
                               (profile, source, id)).fetchone() is not None

    def favourites(self, profile: str) -> set[tuple[str, str]]:
        return {(r["source"], r["id"]) for r in
                self.db.execute("SELECT source, id FROM favourites WHERE profile=?", (profile,))}

    def set_favourite(self, profile: str, keys: list[tuple[str, str]], on: bool) -> None:
        """Start or stop announcing changes to these listings. What is stored now is the baseline."""
        with self.db:
            if on:
                self.db.executemany("INSERT OR IGNORE INTO favourites VALUES (?, ?, ?, ?)",
                                    [(profile, s, i, _now()) for s, i in keys])
            else:
                self.db.executemany("DELETE FROM favourites WHERE profile=? AND source=? AND id=?",
                                    [(profile, s, i) for s, i in keys])

    def _record_changes(self, key: tuple[str, str, str], now: str, changes: list[tuple[str, Any, Any]]) -> None:
        self.db.executemany(
            "INSERT INTO favourite_changes (profile, source, id, seen_at, field, old, new) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(*key, now, f, _dumps(old), _dumps(new)) for f, old, new in changes])

    def unsent_changes(self, profile: str) -> list[dict[str, Any]]:
        """Changes to favourites not yet announced, one entry per listing: its title, url, source,
        the change rows' numbers (`ns`) and `changes` as (field, old, new)."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        rows = self.db.execute(
            """SELECT c.*, l.title, l.url FROM favourite_changes c
               LEFT JOIN listings l ON l.profile=c.profile AND l.source=c.source AND l.id=c.id
               WHERE c.profile=? AND c.sent=0 ORDER BY c.n""", (profile,))
        for r in rows:
            item = out.setdefault((r["source"], r["id"]), {"source": r["source"], "id": r["id"], "title": r["title"],
                                                           "url": r["url"], "ns": [], "changes": []})
            item["ns"].append(r["n"])
            item["changes"].append((r["field"], json.loads(r["old"]), json.loads(r["new"])))
        return list(out.values())

    def mark_changes_sent(self, ns: list[int]) -> None:
        with self.db:
            self.db.executemany("UPDATE favourite_changes SET sent=1 WHERE n=?", [(n,) for n in ns])

    def notified(self, profile: str, channel: str, results: list[Result]) -> bool:
        """Whether `channel` has announced any of these listings (copies of one item) before."""
        return any(self.db.execute(
            "SELECT 1 FROM notifications WHERE profile=? AND channel=? AND source=? AND id=?",
            (profile, channel, r.listing.source, r.listing.id)).fetchone() for r in results)

    def mark_notified(self, profile: str, channel: str, results: list[Result]) -> None:
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO notifications VALUES (?, ?, ?, ?, ?)",
                                [(profile, channel, r.listing.source, r.listing.id, _now()) for r in results])

    def price_history(self, profile: str) -> dict[tuple[str, str], list[tuple[str, Any]]]:
        """(source, id) -> [(seen_at, price), ...] oldest first."""
        out: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        for row in self.db.execute("SELECT * FROM price_history WHERE profile=? ORDER BY seen_at", (profile,)):
            out.setdefault((row["source"], row["id"]), []).append((row["seen_at"], row["price"]))
        return out

    def last_run(self, profile: str) -> dict[str, Any] | None:
        """The latest run of a profile and how each source did on it."""
        run = self.db.execute("SELECT * FROM runs WHERE profile=? ORDER BY id DESC LIMIT 1", (profile,)).fetchone()
        if not run:
            return None
        srcs = self.db.execute("SELECT * FROM run_sources WHERE run_id=? ORDER BY source", (run["id"],)).fetchall()
        sources = []
        for s in srcs:
            s = dict(s)
            s["warnings"] = json.loads(s["warnings"] or "[]")
            s["complete"] = s["complete"] != 0  # NULL (older runs) counts as complete
            sources.append(s)
        return {"started_at": run["started_at"], "sources": sources}

    def report(self, profile: Profile) -> Report:
        """Re-judge every stored listing for this profile against its current rules."""
        domain = domains.get(profile.domain)()
        matches, rejected = [], []
        rows = self.db.execute("SELECT * FROM listings WHERE profile=?", (profile.name,)).fetchall()
        for row in rows:
            fields = json.loads(row["fields"])
            # Undo the stored aliasing so the profile's current `aliases` decide the canonical value.
            for f in profile.aliases:
                if f"{f}_raw" in fields:
                    fields[f] = fields.pop(f"{f}_raw")
            listing = Listing(row["source"], row["id"], row["url"], row["title"], fields, json.loads(row["raw"] or "{}"))
            r = judge(profile, domain, listing)
            r.first_seen, r.last_seen = row["first_seen"], row["last_seen"]
            r.first_price = self._first_price(profile.name, row["source"], row["id"])
            (matches if r.evaluation.passed else rejected).append(r)
        rank(profile, matches)
        return Report(profile, domain, [], dedupe(domain, matches), rejected)
