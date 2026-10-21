"""The one HTTP client: timeouts, capped retries with backoff, size limits, JSON and file downloads."""

import json
import logging
import math
import random
import threading
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from commander import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"Commander/{__version__} (personal deck tracker)"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
SLOW_CALL_SECONDS = 5.0
ERROR_DETAIL_BYTES = 300
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
_SUCCESS = range(200, 300)
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class NetworkError(Exception):
    """A request failed in a way the user should hear about (offline, server error, bad response)."""


class ResponseTooLargeError(NetworkError):
    """The answer ran past the size cap; asking again would get the same answer, so it is not retried."""


class HttpStatusError(NetworkError):
    """The server answered with a status outside 2xx that retrying would not change; callers map some
    statuses to advice (a refused login, a rejected write).

    detail holds the start of the server's answer, which usually says what it rejected. It is kept out of
    the message, so a caller chooses whether to show it.
    """

    def __init__(self, message: str, status: int, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class RateLimitedError(HttpStatusError):
    """The server answered 429 to a request that asked to see it (`get_json(surface_rate_limit=True)`); the
    caller decides how long to stay away.

    retry_after is the server's Retry-After in seconds, if it sent one.
    """

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message, HTTP_TOO_MANY_REQUESTS)
        self.retry_after = retry_after


def make_client() -> httpx.Client:
    # Redirects are not followed, so a response can never come from a host we did not ask for.
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
    )


class Fetcher:
    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
        slow_after: float = SLOW_CALL_SECONDS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._slow_after = slow_after
        self._client = client
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._jitter = jitter

    def get_json(
        self,
        url: str,
        params: Mapping[str, str | int] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        surface_rate_limit: bool = False,
    ) -> object:
        """GET a JSON document, retrying only transient failures.

        With surface_rate_limit a 429 is not retried here but raised as RateLimitedError, for a caller that
        paces its requests for a service with its own rules about how long to wait.
        """
        body = self._send(
            "GET",
            url,
            params,
            None,
            lambda r: _read_capped(r, url, MAX_RESPONSE_BYTES),
            headers=headers,
            surface_rate_limit=surface_rate_limit,
        )
        return _decode_json(url, body)

    def post_json(self, url: str, payload: object, *, headers: Mapping[str, str] | None = None) -> object:
        """POST a JSON query and read the JSON answer; only for read-only queries, as it retries like GET.

        The headers may hold a key, which is never logged or put in an error message.
        """
        body = self._send(
            "POST", url, None, payload, lambda r: _read_capped(r, url, MAX_RESPONSE_BYTES), headers=headers
        )
        return _decode_json(url, body)

    def send_json(
        self, method: str, url: str, payload: object, *, headers: Mapping[str, str] | None = None
    ) -> object:
        """Send a write once, never retried, as repeating it could apply it twice; any 2xx answer counts.

        Returns the decoded answer, or None for an empty one. Other statuses raise HttpStatusError. The
        payload and headers may hold secrets, so neither is ever logged or put in an error message.
        """
        body = self._send_once(
            method, url, payload, headers, lambda r: _read_capped(r, url, MAX_RESPONSE_BYTES)
        )
        return _decode_json(url, body) if body.strip() else None

    def send(
        self, method: str, url: str, payload: object, *, headers: Mapping[str, str] | None = None
    ) -> None:
        """Send a write once like send_json, for callers that ignore the answer: a 2xx counts as done
        whatever its body, which is never read, since a failure here would have the user repeat the write."""
        self._send_once(method, url, payload, headers, lambda _: None)

    def _send_once[T](
        self,
        method: str,
        url: str,
        payload: object,
        headers: Mapping[str, str] | None,
        read_success: Callable[[httpx.Response], T],
    ) -> T:
        started = self._monotonic()
        try:
            with self._client.stream(method, url, json=payload, headers=headers) as response:
                status = response.status_code
                if status not in _SUCCESS:
                    log.info("%s %s returned HTTP %d", method, url, status)
                    detail = _detail_text(_read_start(response, ERROR_DETAIL_BYTES))
                    raise HttpStatusError(f"{method} {url} returned HTTP {status}", status, detail)
                return read_success(response)
        except httpx.RequestError as error:
            log.warning("%s %s failed: %s", method, url, error.__class__.__name__)
            # A timeout leaves it unknown whether the server applied the write.
            raise NetworkError(
                f"{method} {url} failed ({error.__class__.__name__}); it may or may not have been applied"
            ) from error
        except (httpx.InvalidURL, RuntimeError) as error:
            raise self._not_sent(url, error) from error
        finally:
            elapsed = self._monotonic() - started
            if elapsed > self._slow_after:
                log.warning("slow request: %s %s took %.1fs", method, url, elapsed)

    def download(self, url: str, dest: Path, *, max_bytes: int) -> int:
        """Stream a file into dest (replacing it), retrying only transient failures; returns its size."""
        return self._send("GET", url, None, None, lambda r: _write_capped(r, url, dest, max_bytes))

    def _send[T](
        self,
        method: str,
        url: str,
        params: Mapping[str, str | int] | None,
        payload: object,
        read: Callable[[httpx.Response], T],
        *,
        headers: Mapping[str, str] | None = None,
        surface_rate_limit: bool = False,
    ) -> T:
        for attempt in range(1, self._max_attempts + 1):
            started = self._monotonic()
            try:
                with self._client.stream(
                    method, url, params=params, json=payload, headers=headers
                ) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    value = read(response) if status == HTTP_OK else None
            except (httpx.TimeoutException, httpx.TransportError) as error:
                self._give_up_or_wait(attempt, url, str(error) or type(error).__name__, None)
                continue
            except httpx.RequestError as error:
                # A body that cannot be decoded or a redirect loop will fail the same way again.
                log.warning("request failed: %s (%s)", url, error.__class__.__name__)
                raise NetworkError(f"{url} failed ({error.__class__.__name__})") from error
            except (httpx.InvalidURL, RuntimeError) as error:
                raise self._not_sent(url, error) from error
            elapsed = self._monotonic() - started
            if elapsed > self._slow_after:
                log.warning("slow request: %s took %.1fs", url, elapsed)
            if value is not None:
                return value
            if status == HTTP_TOO_MANY_REQUESTS and surface_rate_limit:
                log.info("rate limited: %s", url)
                raise RateLimitedError(
                    f"{url} returned HTTP {status}", _retry_after_seconds(retry_after, self._wall_clock())
                )
            if status in _RETRY_STATUSES:
                self._give_up_or_wait(attempt, url, f"HTTP {status}", retry_after)
                continue
            raise HttpStatusError(f"{url} returned HTTP {status}", status)
        raise AssertionError("unreachable: the last attempt raises")  # pragma: no cover

    def _not_sent(self, url: str, error: Exception) -> NetworkError:
        """The error for a request that never went out: a bad URL, or a client closed under a running sync.

        Any other RuntimeError is a programming error and is raised as it is.
        """
        if isinstance(error, RuntimeError) and not self._client.is_closed:
            raise error
        log.warning("request not sent: %s (%s)", url, error.__class__.__name__)
        reason = "the HTTP client was closed" if isinstance(error, RuntimeError) else "invalid URL"
        return NetworkError(f"{url} not requested: {reason} ({error.__class__.__name__})")

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


class Pacer:
    """Spaces calls at least `interval` seconds apart, across threads, for services that ask for unhurried
    requests."""

    def __init__(
        self,
        interval: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval < 0:
            raise ValueError("interval must not be negative")
        self._interval = interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None
        self._not_before = -math.inf
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until the next call may go out, then count it as sent."""
        # Sleeping under the lock queues the other threads behind this call, each its interval later.
        with self._lock:
            now = self._monotonic()
            ready = self._not_before
            if self._last is not None:
                ready = max(ready, self._last + self._interval)
            remaining = ready - now
            if remaining > 0:
                self._sleep(remaining)
                now += remaining
            self._last = now

    def cool_down(self, seconds: float) -> None:
        """Hold every call from now until `seconds` have passed, for a service that asked for quiet."""
        with self._lock:
            self._not_before = max(self._not_before, self._monotonic() + max(seconds, 0.0))


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


def _read_capped(response: httpx.Response, url: str, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLargeError(f"{url} sent more than {_megabytes(max_bytes)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _write_capped(response: httpx.Response, url: str, dest: Path, max_bytes: int) -> int:
    size = 0
    with dest.open("wb") as handle:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError(f"{url} sent more than {_megabytes(max_bytes)} MB")
            handle.write(chunk)
    return size


def _read_start(response: httpx.Response, limit: int) -> bytes:
    start = b""
    for chunk in response.iter_bytes():
        start += chunk
        if len(start) >= limit:
            break
    return start[:limit]


def _detail_text(body: bytes) -> str:
    # One line of printable text, so a hostile or HTML answer cannot garble the terminal.
    text = body.decode("utf-8", errors="replace")
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def _megabytes(size: int) -> int:
    return size // 1024 // 1024


def _decode_json(url: str, body: bytes) -> object:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        # Services sometimes answer 200 with an HTML error page.
        raise NetworkError(f"{url} did not return JSON ({error.__class__.__name__})") from error
