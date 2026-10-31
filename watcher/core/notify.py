"""Send a message about top-scoring matches after a run.

A profile opts in with a `notify` block:

    notify:
      channels: [messenger]   # see watcher/notifiers
      min_score: 5            # optional; default is full marks on every optional rule

Each car is announced once: what was sent is recorded in the database's `notifications` table,
so the next run skips it. A channel that isn't set up (no keys yet) sends nothing and records
nothing, so the matches are still announced once it is.

Favourites (starred on the web page) are different: every change to one is announced, whatever
its score, as soon as a run sees it; see `notify_favourites`.

Channels that can also receive (`poll_seconds` > 0) let `watcher web` refresh on request:
any message you send it starts a run of every profile that notifies through that channel.

Keys and IDs come from environment variables, which `load_env` also reads from a `.env` file
in the project folder (gitignored; see `.env.example`).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

from .output import fmt
from .registry import Registry
from .runner import Report, Result
from .source import sources

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class NotifyError(Exception):
    """A channel couldn't send (bad key, API refused...)."""


class Channel:
    #: Registry key used in a profile's `notify.channels`.
    name: str = ""
    label: str = ""

    def missing(self) -> list[str]:
        """Settings (env var names) still needed before this channel can send."""
        return []

    #: How often `listen` checks for messages; 0 means the channel can't receive.
    poll_seconds: float = 0

    def send(self, text: str) -> None:
        raise NotImplementedError

    def received(self) -> list[tuple[str, str]]:
        """Recent (message id, text) the recipient sent us, newest first."""
        return []


channels: Registry[Channel] = Registry("notify channel", "watcher.notifiers")
register_channel = channels.register


def load_env(path: Path = ENV_FILE) -> None:
    """Copy KEY=value lines from `.env` into os.environ (real environment variables win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def listen(channel: Channel, on_message: Callable[[list[str]], None],
           say: Callable[[str], None] = print, stop: threading.Event | None = None) -> None:
    """Call `on_message` with the texts of messages that arrive after this starts (blocks)."""
    stop = stop or threading.Event()
    seen: set[str] | None = None  # messages already there when we started don't count
    while not stop.is_set():
        try:
            msgs = channel.received()
            new = [text for mid, text in msgs if seen is not None and mid not in seen]
            seen = (seen or set()) | {mid for mid, _ in msgs}
            if new:
                on_message(new)
        except Exception as e:  # noqa: BLE001 - keep listening through network blips
            say(f"{channel.label or channel.name}: checking for messages failed - {e}")
        stop.wait(channel.poll_seconds)


def threshold(report: Report) -> float | None:
    """The score a match needs to be announced, or None if the profile doesn't notify."""
    cfg = report.profile.notify
    if not cfg.get("channels"):
        return None
    if cfg.get("min_score") is not None:
        return float(cfg["min_score"])
    return float(sum(c.weight for c in report.profile.optional))


def describe(m: Result, report: Report) -> str:
    """One match as a few plain-text lines: title, score, key fields and every link."""
    p, domain, ev = report.profile, report.domain, m.evaluation
    cols = [c for c in (p.display or domain.display) if m.listing.fields.get(c) is not None]
    lines = [f"{m.listing.title} ({ev.score:g}/{ev.max_score:g})",
             " · ".join(fmt(c, m.listing.fields[c], domain) for c in cols)]
    if m.first_price is not None and m.first_price != m.listing.fields.get("price"):
        lines[-1] += f" (was {fmt('price', m.first_price, domain)})"
    lines += [c.listing.url for c in m.copies()]
    return "\n".join(lines)


def compose(matches: list[Result], report: Report) -> str:
    head = f"{report.profile.name}: {len(matches)} new top match" + ("es" if len(matches) > 1 else "")
    return "\n\n".join([head, *(describe(m, report) for m in matches)])


def describe_changes(item: dict[str, Any], report: Report) -> str:
    """A favourite's title, a line per change (old -> new), then its link."""
    domain = report.domain
    lines = [f"★ {item['title']} ({sources.get(item['source']).label or item['source']})"]
    for field, old, new in item["changes"]:
        if field == "photo":
            lines.append("📷 Main photo added - photos are up!" if old is None
                         else "📷 Main photo removed" if new is None else "📷 Main photo changed")
        elif field in ("title", "url"):
            lines.append(f"{field.capitalize()} changed")
        else:
            lines.append(f"{field.replace('_', ' ').capitalize()}: {fmt(field, old, domain)} → {fmt(field, new, domain)}")
    lines.append(item["url"] or "")
    return "\n".join(lines)


def notify_favourites(report: Report, store: Any, say: Callable[[str], None] = print) -> None:
    """Message every change `store.save` found on a favourite. A change stays queued until some
    channel has sent it, so a channel that isn't set up yet or fails doesn't lose it."""
    items = store.unsent_changes(report.profile.name)
    if not items:
        return
    text = "\n\n".join([f"{report.profile.name}: {len(items)} favourite(s) changed",
                         *(describe_changes(i, report) for i in items)])
    sent = False
    for name in report.profile.notify["channels"]:
        channel = channels.get(name)()
        label = channel.label or channel.name
        missing = channel.missing()
        if missing:
            say(f"{label}: not set up ({', '.join(missing)} missing), {len(items)} favourite change(s) not sent")
            continue
        try:
            channel.send(text)
        except NotifyError as e:
            say(f"{label}: sending failed - {e}")
            continue
        sent = True
        say(f"{label}: sent {len(items)} favourite change(s)")
    if sent:
        store.mark_changes_sent([n for i in items for n in i["ns"]])


def notify(report: Report, store: Any, say: Callable[[str], None] = print) -> None:
    """Announce changes to favourites, then matches scoring at least the profile's threshold that
    haven't been announced. Call after `store.save(report)`."""
    if report.profile.notify.get("channels"):
        notify_favourites(report, store, say)
    need = threshold(report)
    if need is None:
        return
    top = [m for m in report.matches if m.evaluation.score >= need]
    for name in report.profile.notify["channels"]:
        channel = channels.get(name)()
        label = channel.label or channel.name
        todo = [m for m in top if not store.notified(report.profile.name, name, m.copies())]
        if not todo:
            continue
        missing = channel.missing()
        if missing:
            say(f"{label}: not set up ({', '.join(missing)} missing), {len(todo)} match(es) not sent")
            continue
        try:
            channel.send(compose(todo, report))
        except NotifyError as e:
            say(f"{label}: sending failed - {e}")
            continue
        store.mark_notified(report.profile.name, name, [c for m in todo for c in m.copies()])
        say(f"{label}: sent {len(todo)} match(es)")
