"""Base class every website integration extends."""
from __future__ import annotations

from typing import Any

from .models import FetchContext, Listing
from .registry import Registry


class Source:
    #: Registry key used in profiles, e.g. "cars24".
    name: str = ""
    #: Human-friendly label for output.
    label: str = ""
    #: Domain this source produces listings for (see watcher/domains).
    domain: str = "generic"
    #: Free-text notes on how the site was found to work (shown by `sources`/`investigate`).
    notes: str = ""

    def __init__(self, options: dict[str, Any] | None = None):
        # Per-source options from the profile's `source_options.<name>` block.
        self.options = options or {}

    def search_urls(self, search: dict[str, Any]) -> list[str]:
        """Human-facing URLs for this search. Used by `investigate` and shown in output."""
        return []

    def fetch(self, search: dict[str, Any], ctx: FetchContext) -> list[Listing]:
        raise NotImplementedError

    def image(self, raw: dict[str, Any]) -> str | None:
        """A photo URL from a listing's `raw` data, if the site gives one (shown by `web`)."""
        return None


sources: Registry[Source] = Registry("source", "watcher.sources")
register_source = sources.register
