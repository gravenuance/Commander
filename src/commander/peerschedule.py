"""When a cached Archidekt search answer is due again, staggered by group so a sync never refreshes them all.

Each group (a commander at a bracket, or a deck tag) gets a stable day from a hash of its cache item name, so
the schedule needs no stored state and does not move between runs (ADR 0027).
"""

import hashlib
from datetime import UTC, datetime, timedelta

# An answer is at most this old when its group is refreshed: ideas may lag Archidekt by up to three days.
PEER_FRESH_FOR = timedelta(days=3)
# An answer younger than this is never refreshed, so a second sync soon after the first asks for nothing.
MIN_FRESH_FOR = timedelta(hours=24)


def stable_offset(item: str, salt: str, modulus: int) -> int:
    """A number in 0..modulus-1 that depends only on the item and the salt, unlike the built-in hash."""
    digest = hashlib.sha256(f"{salt}:{item}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % modulus


def fresh_for(item: str, now: datetime) -> timedelta:
    """How long ago an answer may have been fetched and still be fresh: back to the start of the group's most
    recent refresh day, one in every PEER_FRESH_FOR.days, but never under a day. An answer fetched since then
    is fresh; an older one is refreshed on that day or, if no sync ran, at the next sync."""
    return max(now - _latest_day_start(item, "refresh", PEER_FRESH_FOR, now), MIN_FRESH_FOR)


def _latest_day_start(item: str, salt: str, period: timedelta, now: datetime) -> datetime:
    """The start (UTC) of the latest day, today or earlier, that is the group's day in a cycle of `period`."""
    days = period.days
    today = now.astimezone(UTC).date()
    back = (today.toordinal() - stable_offset(item, salt, days)) % days
    day = today - timedelta(days=back)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
