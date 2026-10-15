from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from commander.net import MAX_RESPONSE_BYTES, Fetcher, NetworkError

URL = "https://archidekt.com/api/decks/1/"


class Script:
    """Answers each request with the next scripted response or exception."""

    def __init__(self, *steps: httpx.Response | Exception) -> None:
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, _: httpx.Request) -> httpx.Response:
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def fetcher(script: Script, sleeps: list[float], *, now: float = 0.0) -> Fetcher:
    return Fetcher(
        httpx.Client(transport=httpx.MockTransport(script)),
        sleep=sleeps.append,
        wall_clock=lambda: now,
        jitter=lambda: 1.0,
    )


def test_returns_parsed_json() -> None:
    sleeps: list[float] = []
    assert fetcher(Script(httpx.Response(200, json={"ok": True})), sleeps).get_json(URL) == {"ok": True}
    assert sleeps == []


def test_retries_server_errors_with_exponential_backoff() -> None:
    script = Script(httpx.Response(503), httpx.Response(502), httpx.Response(200, json=[1]))
    sleeps: list[float] = []
    assert fetcher(script, sleeps).get_json(URL) == [1]
    # jitter=1.0 gives the full backoff: 1s, then 2s.
    assert sleeps == [1.0, 2.0]


def test_retries_connection_failures() -> None:
    request = httpx.Request("GET", URL)
    script = Script(httpx.ConnectError("refused", request=request), httpx.Response(200, json=[]))
    sleeps: list[float] = []
    assert fetcher(script, sleeps).get_json(URL) == []
    assert script.calls == 2


def test_honours_retry_after_seconds() -> None:
    script = Script(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=[]))
    sleeps: list[float] = []
    fetcher(script, sleeps).get_json(URL)
    assert sleeps == [7.0]


def test_honours_retry_after_http_date() -> None:
    # 2026-10-15 12:00:10 UTC is 10 seconds after the injected clock.
    retry = httpx.Response(429, headers={"Retry-After": "Thu, 15 Oct 2026 12:00:10 GMT"})
    script = Script(retry, httpx.Response(200, json=[]))
    sleeps: list[float] = []
    fetcher(script, sleeps, now=1_792_065_600.0).get_json(URL)
    assert sleeps == [10.0]


def test_caps_retry_after() -> None:
    script = Script(httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200, json=[]))
    sleeps: list[float] = []
    fetcher(script, sleeps).get_json(URL)
    assert sleeps == [30.0]


def test_gives_up_after_max_attempts() -> None:
    script = Script(*(httpx.Response(500) for _ in range(4)))
    sleeps: list[float] = []
    with pytest.raises(NetworkError, match="failed after 4 attempts: HTTP 500"):
        fetcher(script, sleeps).get_json(URL)
    assert script.calls == 4
    assert len(sleeps) == 3


def test_does_not_retry_client_errors() -> None:
    script = Script(httpx.Response(404))
    with pytest.raises(NetworkError, match="HTTP 404"):
        fetcher(script, []).get_json(URL)
    assert script.calls == 1


def test_does_not_follow_redirects() -> None:
    script = Script(httpx.Response(302, headers={"Location": "https://elsewhere.example/"}))
    with pytest.raises(NetworkError, match="HTTP 302"):
        fetcher(script, []).get_json(URL)
    assert script.calls == 1


def test_rejects_html_with_200() -> None:
    script = Script(httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(NetworkError, match="did not return JSON"):
        fetcher(script, []).get_json(URL)


def test_rejects_oversized_response() -> None:
    script = Script(httpx.Response(200, content=b"[" + b" " * MAX_RESPONSE_BYTES + b"]"))
    with pytest.raises(NetworkError, match="more than 20 MB"):
        fetcher(script, []).get_json(URL)


def test_download_streams_the_body_to_a_file(tmp_path: Path) -> None:
    dest = tmp_path / "bulk.gz"
    size = fetcher(Script(httpx.Response(200, content=b"abc")), []).download(URL, dest, max_bytes=10)
    assert (size, dest.read_bytes()) == (3, b"abc")


class DiesMidway(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("connection reset")


def test_download_retries_and_keeps_only_the_successful_body(tmp_path: Path) -> None:
    dest = tmp_path / "bulk.gz"
    script = Script(httpx.Response(200, stream=DiesMidway()), httpx.Response(200, content=b"whole"))
    sleeps: list[float] = []
    fetcher(script, sleeps).download(URL, dest, max_bytes=10)
    assert dest.read_bytes() == b"whole"
    assert sleeps == [1.0]


def test_download_refuses_more_than_the_cap(tmp_path: Path) -> None:
    script = Script(httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)))
    with pytest.raises(NetworkError, match="more than 2 MB"):
        fetcher(script, []).download(URL, tmp_path / "bulk.gz", max_bytes=2 * 1024 * 1024)


def test_download_error_status_writes_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "bulk.gz"
    with pytest.raises(NetworkError, match="HTTP 404"):
        fetcher(Script(httpx.Response(404, content=b"not found")), []).download(URL, dest, max_bytes=10)
    assert not dest.exists()
