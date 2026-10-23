"""SQLite history of everything a profile has found, matched or not.

Tables:
    runs           one row per `watcher run`
    run_sources    how each source did on that run
    listings       latest fields per (profile, source, id), plus first_seen / last_seen
    price_history  one row whenever a listing's price is first seen or changes

A stored listing that a source fetched without problems no longer returns has been taken
down, so its status is set to `sold` (a blocked site says nothing about its stock). It comes
back with its real status if it is relisted.

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
from .runner import Report, dedupe, judge, rank

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
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dumps(v: Any) -> str:
    return json.dumps(v, default=str, ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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
                "INSERT INTO run_sources VALUES (?, ?, ?, ?, ?, ?)",
                [(run_id, s.name, s.status, s.message, s.fetched, s.matched) for s in report.sources])
            for r in [c for m in report.matches for c in m.copies()] + report.rejected:
                lst, ev = r.listing, r.evaluation
                key = (profile, lst.source, lst.id)
                old = self.db.execute("SELECT first_seen, fields FROM listings WHERE profile=? AND source=? AND id=?",
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
                if price is not None and (old is None or price != old_price):
                    self.db.execute("INSERT OR REPLACE INTO price_history VALUES (?, ?, ?, ?, ?)", (*key, now, price))
                r.first_seen = old["first_seen"] if old else now
                r.last_seen = now
                r.is_new = old is None
                r.first_price = self._first_price(*key)
            self._mark_sold(report.profile, [s.name for s in report.sources if s.status == "ok"], now)

    def _mark_sold(self, profile: Profile, ok_sources: list[str], now: str) -> None:
        """Listings an `ok` source didn't return this run have been taken down: assume sold."""
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
            fields["status"] = "sold"
            ev = evaluate(fields, profile.required, profile.optional)
            self.db.execute("UPDATE listings SET fields=?, passed=?, score=? WHERE profile=? AND source=? AND id=?",
                            (_dumps(fields), int(ev.passed), ev.score, profile.name, row["source"], row["id"]))

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
        return {"started_at": run["started_at"], "sources": [dict(s) for s in srcs]}

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
