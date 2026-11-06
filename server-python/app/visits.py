"""Pure helpers for visit de-duplication (no database access).

Why visits are identified by TIME and not by Edge's `visits.id`
---------------------------------------------------------------
Edge's `visits.id` is only unique inside one History file. When Edge rebuilds that file
(profile reset, repair, sync) the numbers start again, and an id-based resume point is
then higher than every new visit: nothing new is ever read again, silently. A visit is
therefore identified by (account, profile, time of the visit, hash of its URL), which
survives such a rebuild. `visit_id` is still sent by the agent, but only as a tie-breaker
and for diagnostics.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


# UrlHash of the MARKER row (never a real URL hash): its VisitTs is the cutoff up to which
# a profile's visits were counted before per-visit rows existed.
ZERO_HASH = "0" * 64


def url_hash(url: str | None) -> str:
    """SHA-256 of the URL as 64 hex characters.

    The URL must already be truncated exactly as it is stored (2048 characters), so the
    same page always gives the same hash. The hex string goes to SQL through
    CONVERT(BINARY(32), <hex>, 2).
    """
    return hashlib.sha256((url or "").encode("utf-8", "replace")).hexdigest()


def local_epoch(dt: datetime, tz: ZoneInfo) -> int:
    """Naive local datetime (the way SQL stores it) -> epoch seconds, UTC.

    An ambiguous time (the repeated hour when DST ends) resolves to its first occurrence.
    """
    return int(dt.replace(tzinfo=tz).timestamp())


def order_visits(visits: Iterable) -> Sequence:
    """Oldest first, `visit_id` breaks ties: the order in which rows are de-duplicated."""
    return sorted(visits, key=lambda v: (v.ts, v.visit_id))


def is_before_cutoff(ts: int, cutoff: int | None) -> bool:
    """True for a visit that an older server version already counted.

    Before dbo.BrowsingVisit existed there were no per-visit rows, so everything up to the
    newest stored visit of a profile at the time of the upgrade (`cutoff`, kept as a marker
    row) counts as ingested. A profile without a cutoff is decided by the rows alone.
    """
    return cutoff is not None and ts <= cutoff
