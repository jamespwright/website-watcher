"""`watcher web`: a small JSON API over the database plus a single-page UI (no build step).

    GET  /api/profiles            profiles the server was started with
    GET  /api/profiles/<id>       a profile's rules + every stored listing, judged against them
    POST /api/profiles/<id>/run   start a scrape of that profile in the background
    GET  /api/run                 state of the current / last background run

Profiles are re-read on every request, so edits to the YAML show up on refresh. Filtering
beyond the profile's own rules, sorting and de-duplication happen in the browser.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import socket
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from watcher.core.profile import Profile, load_profile
from watcher.core.runner import Result, run
from watcher.core.source import sources
from watcher.core.store import Store

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("watcher.web")


@dataclass
class RunState:
    running: bool = False
    profile: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    summary: str | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _rule(c: Any, optional: bool) -> dict[str, Any]:
    out = {"field": c.field, "text": c.describe(), "ops": c.ops}
    if optional:
        out["weight"] = c.weight
    return out


class App:
    def __init__(self, profiles: list[Path], db: Path):
        self.profiles = {p.stem: p for p in profiles}
        self.db = db
        self.state = RunState()
        self.lock = threading.Lock()

    def list_profiles(self) -> list[dict[str, str]]:
        out = []
        for pid, path in self.profiles.items():
            try:
                out.append({"id": pid, "name": load_profile(path).name})
            except Exception as e:  # noqa: BLE001 - one broken YAML shouldn't hide the rest
                out.append({"id": pid, "name": pid, "error": str(e)})
        return out

    def profile(self, pid: str) -> dict[str, Any]:
        p = load_profile(self.profiles[pid])
        store = Store(self.db)
        try:
            report = store.report(p)
            history = store.price_history(p.name)
            last = store.last_run(p.name)
        finally:
            store.close()
        labels = {name: cls.label or name for name, cls in sources.all().items()}
        if last:
            for s in last["sources"]:
                s["label"] = labels.get(s["source"], s["source"])
        results = [c for m in report.matches for c in m.copies()] + report.rejected
        return {
            "id": pid,
            "name": p.name,
            "domain": {
                "name": report.domain.name,
                "display": report.domain.display,
                "money_fields": sorted(report.domain.money_fields),
                "units": report.domain.units,
                "dedupe_keys": report.domain.dedupe_keys,
            },
            "rules": {"required": [_rule(c, False) for c in p.required],
                      "optional": [_rule(c, True) for c in p.optional]},
            "display": p.display or report.domain.display,
            "sort": p.sort,
            "last_run": last,
            "listings": [self._listing(r, labels, history, last) for r in results],
        }

    def _listing(self, r: Result, labels: dict[str, str], history: dict, last: dict | None) -> dict[str, Any]:
        lst, ev = r.listing, r.evaluation
        image = None
        try:
            image = sources.get(lst.source)().image(lst.raw)
        except Exception:  # noqa: BLE001 - a missing photo is cosmetic
            pass
        return {
            "key": f"{lst.source}/{lst.id}",
            "source": lst.source,
            "source_label": labels.get(lst.source, lst.source),
            "id": lst.id,
            "url": lst.url,
            "title": lst.title,
            "image": image,
            "fields": lst.fields,
            "passed": ev.passed,
            "failures": ev.failures,
            "score": ev.score,
            "max_score": ev.max_score,
            "matched": ev.matched,
            "missed": ev.missed,
            "first_seen": r.first_seen,
            "last_seen": r.last_seen,
            "first_price": r.first_price,
            "prices": history.get((lst.source, lst.id), []),
            # Same meaning as the terminal's "new": first found on the latest run.
            "new": bool(last and r.first_seen and r.first_seen >= last["started_at"]),
        }

    def start_run(self, pid: str) -> RunState:
        with self.lock:
            if self.state.running:
                return self.state
            self.state = RunState(running=True, profile=pid, started_at=_now())
        threading.Thread(target=self._run, args=(pid,), daemon=True).start()
        return self.state

    def _run(self, pid: str) -> None:
        state = self.state
        try:
            report = run(load_profile(self.profiles[pid]))
            store = Store(self.db)
            try:
                store.save(report)
            finally:
                store.close()
            new = sum(all(c.is_new for c in m.copies()) for m in report.matches)
            bad = [s.label for s in report.sources if s.status != "ok"]
            state.summary = f"{len(report.matches)} match(es), {new} new" + (
                f"; not checked: {', '.join(bad)}" if bad else "")
        except Exception as e:  # noqa: BLE001 - report it to the page instead of dying silently
            log.exception("web run failed")
            state.error = f"{type(e).__name__}: {e}"
        finally:
            state.finished_at = _now()
            state.running = False


def _handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

        def _json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, default=str, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path: str) -> None:
            file = (STATIC / (path.lstrip("/") or "index.html")).resolve()
            if not file.is_relative_to(STATIC.resolve()) or not file.is_file():
                file = STATIC / "index.html"  # unknown paths get the app
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _profile_id(self, parts: list[str]) -> str | None:
            if len(parts) >= 3 and parts[2] in app.profiles:
                return parts[2]
            self._json({"error": "unknown profile"}, HTTPStatus.NOT_FOUND)
            return None

        def do_GET(self) -> None:
            parts = urlparse(self.path).path.strip("/").split("/")
            try:
                if parts[:2] == ["api", "profiles"] and len(parts) == 2:
                    self._json(app.list_profiles())
                elif parts[:2] == ["api", "profiles"]:
                    if (pid := self._profile_id(parts)) is not None:
                        self._json(app.profile(pid))
                elif parts == ["api", "run"]:
                    self._json(asdict(app.state))
                elif parts[0] == "api":
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._static(urlparse(self.path).path)
            except Exception as e:  # noqa: BLE001
                log.exception("request failed")
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts[:2] == ["api", "profiles"] and parts[-1] == "run" and len(parts) == 4:
                if (pid := self._profile_id(parts)) is not None:
                    self._json(asdict(app.start_run(pid)), HTTPStatus.ACCEPTED)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def _lan_addresses() -> list[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outbound interface
            return [s.getsockname()[0]]
    except OSError:
        return []


def serve(profiles: list[Path], db: Path, host: str, port: int, say=print) -> None:
    server = ThreadingHTTPServer((host, port), _handler(App(profiles, db)))
    urls = [f"http://localhost:{port}"]
    if host in ("0.0.0.0", ""):
        urls += [f"http://{ip}:{port}" for ip in _lan_addresses()]
    say("Serving " + ", ".join(p.stem for p in profiles) + " on " + "  ".join(urls) + "   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
