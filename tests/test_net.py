import json
import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from commander.fetchpriority import fetching_deck
from commander.net import (
    ERROR_DETAIL_BYTES,
    MAX_RESPONSE_BYTES,
    Fetcher,
    HttpStatusError,
    NetworkError,
    Pacer,
    RateLimitedError,
    ResponseTooLargeError,
    send_paced,
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


def test_a_surfaced_429_logs_the_servers_retry_after_with_the_url(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="commander.net"):
        for headers in ({"Retry-After": "300"}, {"Retry-After": "Thu, 15 Oct 2026 12:00:10 GMT"}, {}):
            script = Script(httpx.Response(429, headers=headers))
            with pytest.raises(RateLimitedError):
                fetcher(script, []).get_json(URL, surface_rate_limit=True)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        f"rate limited: {URL} (Retry-After: 300)",
        f"rate limited: {URL} (Retry-After: Thu, 15 Oct 2026 12:00:10 GMT)",
        f"rate limited: {URL} (Retry-After: none)",
    ]


def test_a_surfaced_429_carries_the_wait_from_a_json_body_or_the_longer_of_it_and_the_header() -> None:
    body = {"error": "Too many requests", "retryAfterSeconds": 17}
    for response, expected in (
        (httpx.Response(429, json=body), 17.0),
        (httpx.Response(429, json=body, headers={"Retry-After": "11"}), 17.0),
        (httpx.Response(429, json=body, headers={"Retry-After": "30"}), 30.0),
        (httpx.Response(429, json={"retryAfterSeconds": "soon"}), None),
        (httpx.Response(429, content=b"<html>slow down</html>"), None),
    ):
        with pytest.raises(RateLimitedError) as caught:
            fetcher(Script(response), []).post_json(URL, {}, surface_rate_limit=True)
        assert caught.value.retry_after == expected


def test_send_paced_gives_up_on_a_429_that_always_asks_for_no_wait() -> None:
    script = Script(*(httpx.Response(429, headers={"Retry-After": "0"}) for _ in range(400)))
    sleeps: list[float] = []
    now = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    pacer = Pacer(0.0, sleep=sleep, monotonic=lambda: now[0])
    web = fetcher(script, [])
    with pytest.raises(NetworkError, match="Test is still rate limited after waiting 300 s"):
        send_paced(pacer, lambda: web.get_json(URL, surface_rate_limit=True), what="Test")
    # Each 429 is waited out for at least a second, so 300 of them use up the 300 seconds allowed.
    assert script.calls == 301
    assert sleeps == [1.0] * 300


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
    with pytest.raises(ResponseTooLargeError, match="more than 20 MB"):
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
    with pytest.raises(ResponseTooLargeError, match="more than 2 MB"):
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


class PacerRig:
    """A pacer on a fake clock whose every sleep waits for the test, so each slot is handed over one at a
    time; each requester is a thread asking as the deck it is given (None: no deck)."""

    def __init__(self, ranks: dict[int, int] | None = None, *, interval: float = 2.0) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.granted: list[tuple[int | str | None, float]] = []
        self.ranks = ranks or {}
        self._entered = threading.Semaphore(0)
        self._proceed = threading.Semaphore(0)
        self.threads: dict[int | str | None, threading.Thread] = {}
        self.pacer = Pacer(interval, sleep=self._sleep, monotonic=lambda: self.now)
        if ranks is not None:
            self.pacer.rank_by(lambda deck: self.ranks[deck])

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._entered.release()
        assert self._proceed.acquire(timeout=30)
        self.now += seconds

    def request(self, deck: int | None, *, label: str | None = None) -> None:
        name = deck if label is None else label

        def ask() -> None:
            fetching_deck.set(deck)
            self.pacer.wait()
            self.granted.append((name, self.now))

        self.threads[name] = threading.Thread(target=ask, daemon=True)
        self.threads[name].start()

    def until_a_requester_sleeps(self) -> None:
        assert self._entered.acquire(timeout=30)

    def until_waiting(self, count: int) -> None:
        for _ in range(100_000):
            if self.pacer.waiting == count:
                return
            threading.Event().wait(0.001)
        raise AssertionError(f"{self.pacer.waiting} waiting, not {count}")

    def wake_the_sleeper(self) -> None:
        self._proceed.release()

    def wake_the_sleeper_and_wait_for(self, name: int | str | None) -> None:
        """Let the sleeper wake; the requester `name` is the one that should take the slot."""
        self.wake_the_sleeper()
        self.threads[name].join(timeout=30)
        assert not self.threads[name].is_alive()


def holding_three(rig: PacerRig) -> None:
    """The first slot taken by a caller with no deck, then decks 1, 2 and 3 asking in that order, the first
    one sleeping for the next slot while the other two queue."""
    rig.pacer.wait()
    rig.request(1)
    rig.until_a_requester_sleeps()
    rig.request(2)
    rig.request(3)
    rig.until_waiting(3)


def test_pacer_hands_each_slot_to_the_best_ranked_waiter_not_the_first_to_ask() -> None:
    rig = PacerRig({1: 3, 2: 1, 3: 2})
    holding_three(rig)
    rig.wake_the_sleeper_and_wait_for(2)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(3)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(1)
    # Each slot a full interval after the last, whoever takes it.
    assert rig.granted == [(2, 2.0), (3, 4.0), (1, 6.0)]
    assert rig.sleeps == [2.0, 2.0, 2.0]


def test_pacer_ranks_the_waiters_again_when_the_ranking_changes() -> None:
    rig = PacerRig({1: 3, 2: 1, 3: 2})
    holding_three(rig)
    # Deck 2 was the best; now deck 3 is, then deck 1, then deck 2.
    rig.ranks = {1: 2, 2: 3, 3: 1}
    rig.wake_the_sleeper_and_wait_for(3)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(1)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(2)
    assert [name for name, _ in rig.granted] == [3, 1, 2]


def test_pacer_serves_callers_with_no_deck_ahead_of_decks_and_in_the_order_they_asked() -> None:
    rig = PacerRig({1: 1})
    rig.pacer.wait()
    rig.request(1)
    rig.until_a_requester_sleeps()
    rig.request(None, label="first")
    rig.request(None, label="second")
    rig.until_waiting(3)
    rig.wake_the_sleeper_and_wait_for("first")
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for("second")
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(1)
    assert rig.granted == [("first", 2.0), ("second", 4.0), (1, 6.0)]


def test_pacer_without_a_ranking_serves_everyone_in_the_order_they_asked() -> None:
    rig = PacerRig(None)
    holding_three(rig)
    rig.wake_the_sleeper_and_wait_for(1)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(2)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(3)
    assert [name for name, _ in rig.granted] == [1, 2, 3]
    assert rig.sleeps == [2.0, 2.0, 2.0]


def test_pacer_cool_down_holds_every_waiter_whatever_its_rank() -> None:
    rig = PacerRig({1: 3, 2: 1, 3: 2})
    holding_three(rig)
    # A 429 arrives while deck 1 sleeps for its slot at 2.0: nothing goes out before 10.0.
    rig.pacer.cool_down(10.0)
    rig.wake_the_sleeper()
    # Deck 2 is next, and sleeps what is left of the cool-down; the slots after it are an interval apart.
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(2)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(3)
    rig.until_a_requester_sleeps()
    rig.wake_the_sleeper_and_wait_for(1)
    assert rig.granted == [(2, 10.0), (3, 12.0), (1, 14.0)]
    assert rig.sleeps == [2.0, 8.0, 2.0, 2.0]


def test_pacer_refuses_a_negative_interval() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        Pacer(-1.0)


def test_a_runtime_error_other_than_a_closed_client_is_not_swallowed() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        fetcher(Script(RuntimeError("boom")), []).get_json(URL)
