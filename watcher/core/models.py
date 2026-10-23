"""Generic data types shared by every source and domain."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import logging


@dataclass
class Listing:
    """One item found on a website. `fields` holds normalised, comparable values."""

    source: str
    id: str
    url: str
    title: str
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class SourceUnavailable(Exception):
    """A source could not produce results for a known, expected reason."""

    kind = "unavailable"

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SourceBlocked(SourceUnavailable):
    """The website refused the request (bot protection, captcha, 403...)."""

    kind = "blocked"


class SourceNotImplemented(SourceUnavailable):
    """The source is registered but scraping it is not built yet."""

    kind = "skipped"


@dataclass
class FetchContext:
    """Handed to Source.fetch(): shared HTTP client plus a place to report warnings."""

    http: Any
    warnings: list[str] = field(default_factory=list)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("watcher"))

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.log.debug(message)
