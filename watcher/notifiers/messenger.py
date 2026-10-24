"""Facebook Messenger, sent from a Facebook Page to one person through Meta's Send API.

Setup (once):
  1. A Facebook Page to send from, and a Meta app (developers.facebook.com, type "Business")
     with the Messenger product added and the page connected. While the app is in development
     mode it can only message people who have a role on the app, which is fine for yourself.
  2. Generate a Page access token in the app's Messenger settings -> MESSENGER_PAGE_TOKEN.
  3. Send the page any message from your own Messenger, then run `watcher notify ids`: it lists
     the people who have messaged the page with their page-scoped IDs -> MESSENGER_RECIPIENT_ID.
  4. `watcher notify test` sends a test message.

While `watcher web` is running it checks the conversation every MESSENGER_POLL_SECONDS (default
30, 0 turns it off) and any message you send the page starts a refresh. Polling needs no public
URL, unlike Meta's webhooks.

Meta only delivers ordinary messages within 24 hours of the person last messaging the page.
Messages go out untagged first; if Meta says that window has closed, they are retried with the
ACCOUNT_UPDATE tag (MESSENGER_TAG changes it, empty turns the retry off). A new development-mode
app had that tag refused ("Invalid parameter", subcode 1893061, Oct 2026), apparently until Meta
approves it in App Review, so until then message the page now and then to keep the window open.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from watcher.core.notify import Channel, NotifyError, register_channel

API_VERSION = "v23.0"
MAX_TEXT = 2000  # Send API limit per message
OUTSIDE_WINDOW = 2018278  # error_subcode: more than 24 hours since the person messaged the page


def _chunks(text: str, size: int = MAX_TEXT) -> list[str]:
    """Split on blank lines (between cars) so a car isn't cut in half when it can be avoided."""
    out: list[str] = []
    for part in text.split("\n\n"):
        while len(part) > size:
            out.append(part[:size])
            part = part[size:]
        if out and len(out[-1]) + 2 + len(part) <= size:
            out[-1] += "\n\n" + part
        else:
            out.append(part)
    return out


@register_channel
class Messenger(Channel):
    name = "messenger"
    label = "Messenger"

    def __init__(self) -> None:
        self.token = os.environ.get("MESSENGER_PAGE_TOKEN", "")
        self.recipient = os.environ.get("MESSENGER_RECIPIENT_ID", "")
        self.tag = os.environ.get("MESSENGER_TAG", "ACCOUNT_UPDATE")
        self.poll_seconds = float(os.environ.get("MESSENGER_POLL_SECONDS") or 30)
        self.api = f"https://graph.facebook.com/{os.environ.get('MESSENGER_API_VERSION') or API_VERSION}"

    def missing(self) -> list[str]:
        return [k for k, v in (("MESSENGER_PAGE_TOKEN", self.token),
                               ("MESSENGER_RECIPIENT_ID", self.recipient)) if not v]

    def _call(self, method: str, path: str, params: dict[str, str] | None = None, **kw: Any) -> dict[str, Any]:
        try:
            resp = requests.request(method, f"{self.api}/{path}", params={"access_token": self.token, **(params or {})},
                                    timeout=20, **kw)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise NotifyError(f"{type(e).__name__}: {e}") from e
        if "error" in data:
            err = data["error"]
            e = NotifyError(f"{err.get('message')} (code {err.get('code')}"
                            + (f", subcode {err['error_subcode']}" if err.get("error_subcode") else "") + ")")
            e.subcode = err.get("error_subcode")
            raise e
        return data

    def send(self, text: str) -> None:
        for chunk in _chunks(text):
            body: dict[str, Any] = {"recipient": {"id": self.recipient}, "message": {"text": chunk}}
            try:
                self._call("POST", "me/messages", json={**body, "messaging_type": "UPDATE"})
            except NotifyError as e:
                if getattr(e, "subcode", None) != OUTSIDE_WINDOW or not self.tag:
                    raise
                try:
                    self._call("POST", "me/messages", json={**body, "messaging_type": "MESSAGE_TAG", "tag": self.tag})
                except NotifyError as tagged:
                    raise NotifyError(f"more than 24 hours since you last messaged the page, and the "
                                      f"{self.tag} tag was refused: {tagged}. Message the page, then run again") from e

    def received(self) -> list[tuple[str, str]]:
        data = self._call("GET", "me/conversations", {
            "platform": "messenger", "user_id": self.recipient,
            "fields": "messages.limit(10){message,from}"})
        return [(m["id"], m.get("message") or "")
                for conv in data.get("data", []) for m in conv.get("messages", {}).get("data", [])
                if m.get("from", {}).get("id") == self.recipient]

    def people(self) -> list[tuple[str, str]]:
        """(name, page-scoped ID) of everyone in the page's Messenger conversations, the page included
        (looking the page itself up needs pages_read_engagement, which a plain Messenger token lacks)."""
        data = self._call("GET", "me/conversations", {"platform": "messenger", "fields": "participants"})
        seen: dict[str, str] = {}
        for conv in data.get("data", []):
            for p in conv.get("participants", {}).get("data", []):
                seen[p["id"]] = p.get("name", "?")
        return [(name, pid) for pid, name in seen.items()]
