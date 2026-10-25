"""`watcher web`: a small JSON API over the database plus a single-page UI (no build step).

    GET  /api/profiles            profiles the server was started with
    GET  /api/profiles/<id>       a profile's rules + every stored listing, judged against them
    POST /api/profiles/<id>/run   start a scrape of that profile in the background
    GET  /api/run                 state of the current / last background run

Any message sent to a profile's notify channel (e.g. Messenger) also starts a run; see
watcher/core/notify.py. Scheduled runs (`--schedule`) message a summary to those channels when
done. Profiles are re-read on every request, so edits to the YAML show up on refresh. Filtering
beyond the profile's own rules, sorting and de-duplication happen in the browser.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import socket
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from watcher.core.notify import Channel, NotifyError, channels, listen, notify
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
    # One per site: label, status (ok | blocked | skipped | unavailable | error), fetched, matched, message.
    sites: list[dict[str, Any]] = field(default_factory=list)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _site_line(site: dict[str, Any]) -> str:
    """"✅ Carsales: 42 fetched, 3 matched" or "❌ Drive: blocked - HTTP 403"."""
    if site["status"] == "ok":
        return f"✅ {site['label']}: {site['fetched']} fetched, {site['matched']} matched"
    reason = site["message"] if len(site["message"]) <= 100 else site["message"][:99] + "…"
    return f"❌ {site['label']}: {site['status']}" + (f" - {reason}" if reason else "")


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
        self.start_runs([pid])
        return self.state

    def start_runs(self, pids: list[str], then: Callable[[list[RunState]], None] | None = None) -> bool:
        """Run the profiles one after another in the background; False if a run is already going."""
        with self.lock:
            if self.state.running:
                return False
            self.state = RunState(running=True, profile=pids[0], started_at=_now())
        threading.Thread(target=self._runs, args=(pids, then), daemon=True).start()
        return True

    def _runs(self, pids: list[str], then: Callable[[list[RunState]], None] | None) -> None:
        done = []
        for i, pid in enumerate(pids):
            if i:
                self.state = RunState(running=True, profile=pid, started_at=_now())
            self._run(pid, last=i == len(pids) - 1)
            done.append(self.state)
        if then:
            then(done)

    def _run(self, pid: str, last: bool = True) -> None:
        state = self.state
        try:
            report = run(load_profile(self.profiles[pid]))
            store = Store(self.db)
            try:
                store.save(report)
                notify(report, store, say=log.warning)
            finally:
                store.close()
            new = sum(all(c.is_new for c in m.copies()) for m in report.matches)
            state.summary = f"{len(report.matches)} match(es), {new} new"
            state.sites = [{"label": s.label, "status": s.status, "fetched": s.fetched,
                            "matched": s.matched, "message": s.message} for s in report.sources]
        except Exception as e:  # noqa: BLE001 - report it to the page instead of dying silently
            log.exception("web run failed")
            state.error = f"{type(e).__name__}: {e}"
        finally:
            state.finished_at = _now()
            state.running = not last  # stay "running" between profiles so nothing else starts

    def on_message(self, channel: Channel, pids: list[str]) -> None:
        """A message arrived on `channel`: refresh the profiles that notify through it."""
        names = ", ".join(load_profile(self.profiles[p]).name for p in pids)

        def report(states: list[RunState]) -> None:
            self._reply(channel, "Refresh finished.\n" + self.summary(states))

        if self.start_runs(pids, then=report):
            self._reply(channel, f"Refreshing {names}. New top matches will follow, then a summary.")
        else:
            self._reply(channel, "A refresh is already running; new top matches will arrive when it finishes.")

    def summary(self, states: list[RunState]) -> str:
        """Per finished run: profile name and its match counts or error, then a line per site."""
        lines = []
        for s in states:
            lines.append(f"{load_profile(self.profiles[s.profile]).name}: "
                         + (f"failed - {s.error}" if s.error else s.summary or "done"))
            lines += ["  " + _site_line(site) for site in s.sites]
        return "\n".join(lines)

    def by_channel(self) -> dict[str, list[str]]:
        """Notify channel name -> ids of the profiles that notify through it."""
        out: dict[str, list[str]] = {}
        for pid, path in self.profiles.items():
            try:
                for name in load_profile(path).notify.get("channels") or []:
                    out.setdefault(name, []).append(pid)
            except Exception:  # noqa: BLE001 - a broken YAML just doesn't notify
                continue
        return out

    def announce(self, heading: str, states: list[RunState]) -> None:
        """Send `heading` and the summary of the runs to each channel those profiles notify through."""
        for name, pids in self.by_channel().items():
            mine = [s for s in states if s.profile in pids]
            channel = channels.get(name)()
            if mine and not channel.missing():
                self._reply(channel, heading + "\n" + self.summary(mine))

    @staticmethod
    def _reply(channel: Channel, text: str) -> None:
        try:
            channel.send(text)
        except NotifyError as e:
            log.warning("%s: reply failed - %s", channel.label, e)


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


def _start_listeners(app: App, say: Callable[[str], None]) -> None:
    """Poll each notify channel that can receive; a message refreshes the profiles using it."""
    for name, pids in app.by_channel().items():
        channel = channels.get(name)()
        if not channel.poll_seconds or channel.missing():
            continue
        threading.Thread(target=listen, daemon=True, kwargs={
            "channel": channel, "on_message": lambda _texts, c=channel, p=pids: app.on_message(c, p),
            "say": log.warning}).start()
        say(f"Listening on {channel.label}: any message you send refreshes {', '.join(pids)}")


def parse_times(spec: str) -> list[dt_time]:
    """"06:00,12:00,18:00" -> sorted times of day (local time)."""
    return sorted(dt_time.fromisoformat(t.strip()) for t in spec.split(",") if t.strip())


def _next_at(times: list[dt_time], now: datetime) -> datetime:
    today = [datetime.combine(now.date(), t) for t in times]
    return next((t for t in today if t > now), today[0] + timedelta(days=1))


def _start_schedule(app: App, times: list[dt_time], say: Callable[[str], None]) -> None:
    """Run every profile at each of `times`, every day, then message a summary to their notify channels."""
    def loop() -> None:
        while True:
            at = _next_at(times, datetime.now())
            threading.Event().wait((at - datetime.now()).total_seconds())
            heading = f"Scheduled refresh ({at:%H:%M}) finished."
            if not app.start_runs(list(app.profiles), then=lambda states, h=heading: app.announce(h, states)):
                log.warning("scheduled run at %s skipped: a run is already going", at.strftime("%H:%M"))

    threading.Thread(target=loop, daemon=True).start()
    say("Scheduled runs of every profile at " + ", ".join(t.strftime("%H:%M") for t in times))


def serve(profiles: list[Path], db: Path, host: str, port: int, say=print,
          schedule: list[dt_time] | None = None) -> None:
    app = App(profiles, db)
    server = ThreadingHTTPServer((host, port), _handler(app))
    urls = [f"http://localhost:{port}"]
    if host in ("0.0.0.0", ""):
        urls += [f"http://{ip}:{port}" for ip in _lan_addresses()]
    say("Serving " + ", ".join(p.stem for p in profiles) + " on " + "  ".join(urls) + "   (Ctrl+C to stop)")
    _start_listeners(app, say)
    if schedule:
        _start_schedule(app, schedule, say)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
