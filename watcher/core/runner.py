"""Run a profile: fetch from every source, normalise, filter and rank."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .criteria import Evaluation, evaluate
from .domain import Domain, domains
from .http import Http
from .models import FetchContext, Listing, SourceUnavailable
from .profile import Profile
from .source import Source, sources


@dataclass
class SourceResult:
    name: str
    label: str
    status: str  # ok | blocked | skipped | unavailable | error
    message: str = ""
    fetched: int = 0
    matched: int = 0
    warnings: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


@dataclass
class Result:
    listing: Listing
    evaluation: Evaluation
    # Filled in from the database (Store.save / Store.report); None when no store is used.
    first_seen: str | None = None
    last_seen: str | None = None
    first_price: Any = None
    is_new: bool = False
    # The same item listed on other sites (see dedupe); shown on this result's row.
    also: list[Result] = field(default_factory=list)

    def copies(self) -> list[Result]:
        return [self, *self.also]


@dataclass
class Report:
    profile: Profile
    domain: Domain
    sources: list[SourceResult]
    matches: list[Result]
    rejected: list[Result]


def _fetch_one(source: Source, profile: Profile, http: Http) -> tuple[SourceResult, list[Listing]]:
    res = SourceResult(source.name, source.label or source.name, "ok")
    try:
        res.urls = source.search_urls(profile.search)
    except Exception:  # noqa: BLE001 - URLs are informational only
        pass
    ctx = FetchContext(http=http)
    listings: list[Listing] = []
    try:
        listings = source.fetch(profile.search, ctx)
    except SourceUnavailable as e:
        res.status, res.message = e.kind, e.reason
    except Exception as e:  # noqa: BLE001 - one broken site must not kill the run
        res.status, res.message = "error", f"{type(e).__name__}: {e}"
    res.fetched = len(listings)
    res.warnings = ctx.warnings
    return res, listings


def _sort_key(sort: list[str]):
    def key(r: Result) -> tuple:
        parts: list[Any] = []
        for spec in sort:
            desc = spec.startswith("-")
            name = spec.lstrip("-+")
            v = r.evaluation.score if name == "score" else r.listing.fields.get(name)
            missing = v is None
            if isinstance(v, (int, float)):
                v = -v if desc else v
            else:
                v = str(v or "")  # strings sort ascending only
            parts.append((missing, v))
        return tuple(parts)

    return key


def judge(profile: Profile, domain: Domain, listing: Listing) -> Result:
    """Normalise a listing's fields and check them against the profile's rules."""
    listing.fields = profile.canonicalise(domain.normalise(dict(listing.fields)))
    return Result(listing, evaluate(listing.fields, profile.required, profile.optional))


def rank(profile: Profile, results: list[Result]) -> None:
    results.sort(key=_sort_key(profile.sort))


def dedupe(domain: Domain, results: list[Result]) -> list[Result]:
    """Fold listings on different sites whose `domain.dedupe_keys` all match (and are known) into
    the best-ranked one's `also`. Expects `results` already ranked."""
    if not domain.dedupe_keys:
        return results
    out: list[Result] = []
    groups: dict[tuple, Result] = {}
    for r in results:
        key = tuple(r.listing.fields.get(f) for f in domain.dedupe_keys)
        first = groups.get(key) if None not in key else None
        if first and r.listing.source not in {c.listing.source for c in first.copies()}:
            first.also.append(r)
            continue
        out.append(r)
        if None not in key:
            groups.setdefault(key, r)
    return out


def run(profile: Profile, http: Http | None = None) -> Report:
    http = http or Http()
    domain = domains.get(profile.domain)()
    srcs = [sources.get(n)(profile.source_options.get(n)) for n in profile.sources]

    with ThreadPoolExecutor(max_workers=max(1, len(srcs))) as pool:
        fetched = list(pool.map(lambda s: _fetch_one(s, profile, http), srcs))

    matches: list[Result] = []
    rejected: list[Result] = []
    seen: set[tuple[str, str]] = set()
    for res, listings in fetched:
        for listing in listings:
            if (listing.source, listing.id) in seen:
                continue
            seen.add((listing.source, listing.id))
            r = judge(profile, domain, listing)
            (matches if r.evaluation.passed else rejected).append(r)
            res.matched += r.evaluation.passed

    rank(profile, matches)
    return Report(profile, domain, [r for r, _ in fetched], dedupe(domain, matches), rejected)
