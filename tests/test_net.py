import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from commander.net import (
    ERROR_DETAIL_BYTES,
    MAX_RESPONSE_BYTES,
    Fetcher,
    HttpStatusError,
    NetworkError,
    Pacer,
    RateLimitedError,
)

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


def test_a_caller_that_asks_sees_the_429_and_its_retry_after_unretried() -> None:
    sleeps: list[float] = []
    script = Script(httpx.Response(429, headers={"Retry-After": "300"}))
    with pytest.raises(RateLimitedError) as caught:
        fetcher(script, sleeps).get_json(URL, surface_rate_limit=True)
    assert (caught.value.status, caught.value.retry_after) == (429, 300.0)
    assert (script.calls, sleeps) == (1, [])
    bare = Script(httpx.Response(429))
    with pytest.raises(RateLimitedError) as unsaid:
        fetcher(bare, sleeps).get_json(URL, surface_rate_limit=True)
    assert unsaid.value.retry_after is None


def test_a_caller_that_asks_for_the_429_still_gets_other_failures_retried() -> None:
    script = Script(httpx.Response(503), httpx.Response(200, json=[]))
    sleeps: list[float] = []
    assert fetcher(script, sleeps).get_json(URL, surface_rate_limit=True) == []
    assert (script.calls, len(sleeps)) == (2, 1)


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


def test_a_refused_read_raises_the_status_error_callers_map_to_advice() -> None:
    script = Script(httpx.Response(401))

    with pytest.raises(HttpStatusError, match="HTTP 401") as raised:
        fetcher(script, []).get_json(URL)

    assert raised.value.status == 401
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


class BrokenBody(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"["
        raise httpx.DecodingError("bad gzip data")


def test_an_undecodable_body_is_a_network_error_and_not_retried() -> None:
    script = Script(httpx.Response(200, stream=BrokenBody()), httpx.Response(200, json=[]))

    with pytest.raises(NetworkError, match="DecodingError"):
        fetcher(script, []).get_json(URL)

    assert script.calls == 1


def test_a_malformed_url_is_a_network_error() -> None:
    with pytest.raises(NetworkError, match="InvalidURL"):
        fetcher(Script(), []).get_json("http://[::1/")


def test_a_closed_client_is_a_network_error() -> None:
    client = httpx.Client(transport=httpx.MockTransport(Script()))
    client.close()

    with pytest.raises(NetworkError, match="closed"):
        Fetcher(client).get_json(URL)


def test_a_write_with_an_undecodable_answer_is_a_network_error() -> None:
    script = Script(httpx.Response(200, stream=BrokenBody()))

    with pytest.raises(NetworkError, match="DecodingError"):
        fetcher(script, []).send_json("POST", URL, {})


def test_download_refuses_more_than_the_cap(tmp_path: Path) -> None:
    script = Script(httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)))
    with pytest.raises(NetworkError, match="more than 2 MB"):
        fetcher(script, []).download(URL, tmp_path / "bulk.gz", max_bytes=2 * 1024 * 1024)


def test_download_error_status_writes_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "bulk.gz"
    with pytest.raises(NetworkError, match="HTTP 404"):
        fetcher(Script(httpx.Response(404, content=b"not found")), []).download(URL, dest, max_bytes=10)
    assert not dest.exists()


def test_post_sends_the_payload_as_json_and_retries_like_get() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429 if len(seen) == 1 else 200, json={"tag": "R"})

    sleeps: list[float] = []
    client = Fetcher(
        httpx.Client(transport=httpx.MockTransport(answer)), sleep=sleeps.append, jitter=lambda: 1.0
    )
    assert client.post_json(URL, {"main": [{"card": "Sol Ring"}]}) == {"tag": "R"}
    assert [r.method for r in seen] == ["POST", "POST"]
    assert json.loads(seen[1].content) == {"main": [{"card": "Sol Ring"}]}
    assert seen[1].headers["Content-Type"] == "application/json"
    assert sleeps == [1.0]


def test_send_json_goes_out_once_and_takes_any_2xx() -> None:
    sleeps: list[float] = []
    created = Script(httpx.Response(201, json={"id": 1}))
    empty = Script(httpx.Response(204))

    assert fetcher(created, sleeps).send_json("POST", URL, {"a": 1}) == {"id": 1}
    assert fetcher(empty, sleeps).send_json("PATCH", URL, {}) is None
    assert (created.calls, empty.calls, sleeps) == (1, 1, [])


def test_send_takes_any_2xx_without_reading_the_body() -> None:
    html = Script(httpx.Response(200, text="<html>saved</html>"))
    huge = Script(httpx.Response(202, content=b"x" * (MAX_RESPONSE_BYTES + 1)))

    fetcher(html, []).send("POST", URL, {"a": 1})
    fetcher(huge, []).send("PATCH", URL, {})

    assert (html.calls, huge.calls) == (1, 1)


def test_send_raises_the_status_error_and_never_retries() -> None:
    script = Script(httpx.Response(503, text="busy"), httpx.Response(200))
    sleeps: list[float] = []

    with pytest.raises(HttpStatusError, match=r"PATCH .* returned HTTP 503") as raised:
        fetcher(script, sleeps).send("PATCH", URL, {})

    assert (raised.value.status, raised.value.detail, script.calls, sleeps) == (503, "busy", 1, [])


def test_send_says_a_timed_out_write_may_have_landed() -> None:
    with pytest.raises(NetworkError, match="may or may not have been applied"):
        fetcher(Script(httpx.ReadTimeout("slow")), []).send("POST", URL, {})


def test_send_json_never_retries_a_failed_write() -> None:
    sleeps: list[float] = []
    script = Script(httpx.Response(503), httpx.Response(200, json={}))

    with pytest.raises(HttpStatusError, match=r"PATCH .* returned HTTP 503") as raised:
        fetcher(script, sleeps).send_json("PATCH", URL, {})

    assert raised.value.status == 503
    assert (script.calls, sleeps) == (1, [])


def test_send_json_says_a_timed_out_write_may_have_landed() -> None:
    script = Script(httpx.ReadTimeout("slow"))

    with pytest.raises(NetworkError, match="may or may not have been applied"):
        fetcher(script, []).send_json("POST", URL, {"password": "secret"})


def test_send_json_and_get_json_pass_headers() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        Fetcher(client).send_json("POST", URL, {}, headers={"Authorization": "JWT a"})
        Fetcher(client).get_json(URL, headers={"Authorization": "JWT b"})
        Fetcher(client).get_json(URL)

    assert seen == ["JWT a", "JWT b", None]


def test_a_rejected_write_keeps_a_short_printable_excerpt_of_the_answer() -> None:
    answer = b'{"error":\n  "bad\x1b[31m card"}' + b"x" * 1000
    script = Script(httpx.Response(400, content=answer))

    with pytest.raises(HttpStatusError) as raised:
        fetcher(script, []).send_json("PATCH", URL, {})

    detail = raised.value.detail
    assert detail.startswith('{"error": "bad [31m card"}xxx')
    assert len(detail) <= ERROR_DETAIL_BYTES
    assert "x" * 50 not in str(raised.value)


def test_pacer_spaces_calls_apart() -> None:
    now = [10.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    pacer = Pacer(2.0, sleep=sleep, monotonic=lambda: now[0])
    pacer.wait()
    pacer.wait()
    now[0] += 0.5
    pacer.wait()
    now[0] += 5.0
    pacer.wait()
    assert sleeps == [2.0, 1.5]


def test_pacer_cool_down_holds_the_next_call_for_the_longer_of_it_and_the_interval() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    pacer = Pacer(2.0, sleep=sleep, monotonic=lambda: now[0])
    pacer.wait()
    pacer.cool_down(180.0)
    # A shorter cool-down never shortens a longer one.
    pacer.cool_down(30.0)
    pacer.wait()
    assert sleeps == [180.0]
    # Once it is over, the interval alone spaces the calls.
    pacer.wait()
    assert sleeps == [180.0, 2.0]
    # A cool-down shorter than the interval leaves the interval in charge.
    pacer.cool_down(0.5)
    pacer.wait()
    assert sleeps == [180.0, 2.0, 2.0]


def test_pacer_refuses_a_negative_interval() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        Pacer(-1.0)


def test_a_runtime_error_other_than_a_closed_client_is_not_swallowed() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        fetcher(Script(RuntimeError("boom")), []).get_json(URL)
