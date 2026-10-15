"""The one HTTP client: timeouts, capped retries with backoff, size limits and JSON validation."""

import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime

import httpx

from commander import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"Commander/{__version__} (personal deck tracker)"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
SLOW_CALL_SECONDS = 5.0
HTTP_OK = 200
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class NetworkError(Exception):
    """A request failed in a way the user should hear about (offline, server error, bad response)."""


def make_client() -> httpx.Client:
    # Redirects are not followed, so a response can never come from a host we did not ask for.
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
    )


class JsonFetcher:
    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._client = client
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._jitter = jitter

    def get_json(self, url: str, params: Mapping[str, str | int] | None = None) -> object:
        """GET a JSON document, retrying only transient failures."""
        for attempt in range(1, self._max_attempts + 1):
            started = self._monotonic()
            try:
                status, headers, body = self._fetch(url, params)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                self._give_up_or_wait(attempt, url, str(error) or type(error).__name__, None)
                continue
            elapsed = self._monotonic() - started
            if elapsed > SLOW_CALL_SECONDS:
                log.warning("slow request: %s took %.1fs", url, elapsed)
            if status in _RETRY_STATUSES:
                self._give_up_or_wait(attempt, url, f"HTTP {status}", headers.get("Retry-After"))
                continue
            if status != HTTP_OK:
                raise NetworkError(f"{url} returned HTTP {status}")
            return _decode_json(url, body)
        raise AssertionError("unreachable: the last attempt raises")  # pragma: no cover

    def _fetch(self, url: str, params: Mapping[str, str | int] | None) -> tuple[int, httpx.Headers, bytes]:
        with self._client.stream("GET", url, params=params) as response:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise NetworkError(f"{url} sent more than {MAX_RESPONSE_BYTES // 1024 // 1024} MB")
                chunks.append(chunk)
            return response.status_code, response.headers, b"".join(chunks)

    def _give_up_or_wait(self, attempt: int, url: str, reason: str, retry_after: str | None) -> None:
        if attempt >= self._max_attempts:
            log.warning("request failed after %d attempts: %s (%s)", attempt, url, reason)
            raise NetworkError(f"{url} failed after {attempt} attempts: {reason}")
        delay = _retry_after_seconds(retry_after, self._wall_clock())
        if delay is None:
            backoff = BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            delay = backoff * (0.5 + self._jitter() / 2)
        delay = min(delay, MAX_DELAY_SECONDS)
        log.info("retrying %s in %.1fs (%s)", url, delay, reason)
        self._sleep(delay)


def _retry_after_seconds(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        log.info("ignoring unreadable Retry-After header: %r", value)
        return None
    return max(0.0, when.timestamp() - now)


def _decode_json(url: str, body: bytes) -> object:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        # Services sometimes answer 200 with an HTML error page.
        raise NetworkError(f"{url} did not return JSON ({error.__class__.__name__})") from error
