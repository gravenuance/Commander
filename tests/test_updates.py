"""New versions from GitHub Releases (ADR 0059): the check, the download through its redirect, the checksum,
the swap of the exe and the app's bridge."""

import hashlib
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from commander import __version__, updates
from commander.desk import Desk, DeskSettings
from commander.jsonshape import ShapeError
from commander.net import Fetcher, HttpStatusError, Pacer
from commander.updates import (
    LATEST_URL,
    Release,
    UpdateError,
    Updater,
    expected_sha256,
    install,
    parse_release,
    parse_version,
    remove_old_exe,
    restart,
)
from conftest import FakeArchidekt, Json, MemoryTokenStore, load_fixture
from test_desk import NOW, fake, settings

__all__ = ["fake", "settings"]  # the fixtures shared with test_desk

DOWNLOADS = "https://github.com/gravenuance/Commander/releases/download/v1.3.1/"
EXE_URL = f"{DOWNLOADS}Commander.exe"
SUMS_URL = f"{DOWNLOADS}SHA256SUMS.txt"
FILES = "https://release-assets.githubusercontent.com/github-production-release-asset/1/"
NEW_EXE = b"MZ the new exe"
OLD_EXE = b"MZ the running exe"


def sums_for(content: bytes, name: str = "Commander.exe") -> bytes:
    """SHA256SUMS.txt as the release workflow's `sha256sum Commander.exe` writes it."""
    return f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode()


class FakeGitHub:
    """The latest-release answer, and each release file as github.com redirects it to the file host."""

    def __init__(self) -> None:
        # A trimmed real answer for v1.3.1.
        self.release: Json = load_fixture("github_release_latest.json")
        self.release_status = 200
        self.exe = NEW_EXE
        self.sums = sums_for(NEW_EXE)
        self.file_host = FILES
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == LATEST_URL:
            return httpx.Response(self.release_status, json=self.release)
        redirects = {EXE_URL: f"{self.file_host}exe", SUMS_URL: f"{self.file_host}sums"}
        if url in redirects:
            return httpx.Response(302, headers={"Location": redirects[url]})
        files = {f"{FILES}exe": self.exe, f"{FILES}sums": self.sums}
        if url in files:
            return httpx.Response(200, content=files[url])
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def fetcher(fake: FakeGitHub) -> Fetcher:
    return Fetcher(fake.client(), sleep=lambda _: None)


def release() -> Release:
    found = parse_release(load_fixture("github_release_latest.json"))
    assert found is not None
    return found


@pytest.fixture
def exe(tmp_path: Path) -> Path:
    path = tmp_path / "Commander.exe"
    path.write_bytes(OLD_EXE)
    return path


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name not in {"Commander.exe", "Commander.exe.old"})


# --- the release answer


def test_the_real_latest_release_answer_names_its_version_page_and_files() -> None:
    assert release() == Release(
        (1, 3, 1),
        "v1.3.1",
        "https://github.com/gravenuance/Commander/releases/tag/v1.3.1",
        EXE_URL,
        SUMS_URL,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [("v1.4.0", (1, 4, 0)), ("1.10.2", (1, 10, 2)), ("v1.4.0-rc1", None), ("latest", None), ("v1.4", None)],
)
def test_a_version_is_three_numbers_with_or_without_a_v(
    text: str, expected: tuple[int, int, int] | None
) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "change",
    [
        {"draft": True},
        {"prerelease": True},
        {"tag_name": "v1.4.0-rc1"},
        {"assets": []},
        # A file of the same name somewhere else is not this repository's release.
        {
            "assets": [
                {"name": "Commander.exe", "browser_download_url": "https://example.com/Commander.exe"},
                {"name": "SHA256SUMS.txt", "browser_download_url": SUMS_URL},
            ]
        },
    ],
)
def test_a_release_with_nothing_to_update_to_is_not_offered(change: Json) -> None:
    raw = {**load_fixture("github_release_latest.json"), **change}
    assert parse_release(raw) is None


# --- the check


def updater(fake: FakeGitHub, current: str, exe: Path | None = None) -> tuple[Updater, list[Path]]:
    restarted: list[Path] = []
    return Updater(fake.client, current, exe, restart_exe=restarted.append), restarted


def test_a_newer_release_is_offered_and_github_is_asked_once_per_run() -> None:
    fake = FakeGitHub()
    found, _ = updater(fake, "1.3.0")
    assert found.newer_release() == release()
    assert found.newer_release() == release()
    assert fake.requests == [LATEST_URL]


@pytest.mark.parametrize("current", ["1.3.1", "1.4.0", "2.0.0"])
def test_the_same_or_an_older_release_is_not_offered(current: str) -> None:
    assert updater(FakeGitHub(), current)[0].newer_release() is None


def test_a_failed_or_malformed_check_offers_nothing_and_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    failing = FakeGitHub()
    failing.release_status = 404
    malformed = FakeGitHub()
    malformed.release = {"tag_name": 140}
    with caplog.at_level(logging.WARNING, logger="commander.updates"):
        assert updater(failing, "1.3.0")[0].newer_release() is None
        assert updater(malformed, "1.3.0")[0].newer_release() is None
    warned = [m for m in caplog.messages if m.startswith("could not check for a newer version")]
    assert len(warned) == 2
    assert "HTTP 404" in warned[0]
    assert "unexpected release answer" in warned[1]


# --- the install


def test_the_install_puts_the_checked_exe_in_place_and_keeps_the_running_one_aside(exe: Path) -> None:
    fake = FakeGitHub()
    install(fetcher(fake), release(), exe)
    assert exe.read_bytes() == NEW_EXE
    assert (exe.parent / "Commander.exe.old").read_bytes() == OLD_EXE
    assert leftovers(exe.parent) == []
    # Each file through its redirect to GitHub's file host.
    assert fake.requests == [SUMS_URL, f"{FILES}sums", EXE_URL, f"{FILES}exe"]


def test_an_exe_that_does_not_match_its_checksum_changes_nothing(exe: Path) -> None:
    fake = FakeGitHub()
    fake.exe = b"MZ something else"
    with pytest.raises(UpdateError, match="didn't match its checksum; nothing was changed"):
        install(fetcher(fake), release(), exe)
    assert exe.read_bytes() == OLD_EXE
    assert not (exe.parent / "Commander.exe.old").exists()
    assert leftovers(exe.parent) == []


@pytest.mark.parametrize(
    "file_host",
    [
        "https://example.com/",
        # GitHub's file host, but not over https.
        "http://release-assets.githubusercontent.com/github-production-release-asset/1/",
        # A name that only starts like GitHub's file host.
        "https://release-assets.githubusercontent.com.example.net/",
        "https://evil-release-assets.githubusercontent.com/",
    ],
)
def test_a_redirect_away_from_githubs_file_host_is_not_followed(exe: Path, file_host: str) -> None:
    fake = FakeGitHub()
    fake.file_host = file_host
    with pytest.raises(UpdateError, match="unexpected address"):
        install(fetcher(fake), release(), exe)
    assert fake.requests == [SUMS_URL]
    assert exe.read_bytes() == OLD_EXE
    assert leftovers(exe.parent) == []


def test_a_release_page_outside_the_repository_is_a_malformed_answer() -> None:
    raw = {**load_fixture("github_release_latest.json"), "html_url": "https://example.com/releases/v1.3.1"}
    with pytest.raises(ShapeError, match="not a release page"):
        parse_release(raw)


def test_a_failed_download_changes_nothing(exe: Path) -> None:
    fake = FakeGitHub()
    fake.file_host = "https://release-assets.githubusercontent.com/missing/"
    with pytest.raises(UpdateError, match="Couldn't download the update"):
        install(fetcher(fake), release(), exe)
    assert exe.read_bytes() == OLD_EXE
    assert leftovers(exe.parent) == []


def test_a_folder_the_app_cannot_write_to_changes_nothing_and_leaves_no_temp_file(
    exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[Path] = []

    def second_refused(folder: Path, name: str, suffix: str) -> Path:
        if made:
            raise PermissionError("Access is denied")
        made.append(folder / f".{name}.test{suffix}")
        made[-1].touch()
        return made[-1]

    monkeypatch.setattr(updates, "new_temp_path", second_refused)
    with pytest.raises(UpdateError, match=r"Couldn't replace Commander\.exe; is its folder writable\?"):
        install(fetcher(FakeGitHub()), release(), exe)
    assert exe.read_bytes() == OLD_EXE
    assert leftovers(exe.parent) == []


def test_an_update_replaces_the_exe_an_earlier_update_left_aside(exe: Path) -> None:
    (exe.parent / "Commander.exe.old").write_bytes(b"MZ two versions ago")
    install(fetcher(FakeGitHub()), release(), exe)
    assert exe.read_bytes() == NEW_EXE
    assert (exe.parent / "Commander.exe.old").read_bytes() == OLD_EXE


def test_a_new_exe_that_cannot_take_the_running_ones_place_puts_the_running_one_back(
    exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rename = Path.rename

    def refuse_the_new_exe(self: Path, target: Path) -> Path:
        if self.name.endswith(".part"):
            raise PermissionError("locked by a virus scanner")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse_the_new_exe)
    with pytest.raises(UpdateError, match="Couldn't replace"):
        install(fetcher(FakeGitHub()), release(), exe)
    assert exe.read_bytes() == OLD_EXE
    assert not (exe.parent / "Commander.exe.old").exists()


def test_the_updater_installs_then_restarts_the_exe_once_and_refuses_without_one(exe: Path) -> None:
    fake = FakeGitHub()
    found, restarted = updater(fake, "1.3.0", exe)
    assert found.install_and_restart() is True
    assert exe.read_bytes() == NEW_EXE
    assert restarted == [exe]
    # A second click in the same run downloads nothing: this run must end.
    fake.requests.clear()
    with pytest.raises(UpdateError, match="start Commander again"):
        found.install_and_restart()
    assert fake.requests == []
    from_source, never = updater(FakeGitHub(), "1.3.0")
    with pytest.raises(UpdateError, match="Running from source"):
        from_source.install_and_restart()
    assert never == []


def test_an_updated_exe_that_does_not_start_is_logged_and_the_install_stands(
    exe: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_: Path) -> None:
        raise OSError("blocked")

    found = Updater(FakeGitHub().client, "1.3.0", exe, restart_exe=refuse)
    with caplog.at_level(logging.ERROR, logger="commander.updates"):
        assert found.install_and_restart() is False
    assert exe.read_bytes() == NEW_EXE
    assert [m for m in caplog.messages if "did not start" in m]


def test_the_restart_starts_the_exe_on_its_own_with_pyinstallers_environment_reset(
    exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[list[str], dict[str, str], int]] = []

    def popen(args: list[str], *, env: dict[str, str], creationflags: int, close_fds: bool) -> None:
        assert close_fds
        started.append((args, env, creationflags))

    monkeypatch.setattr(subprocess, "Popen", popen)
    restart(exe)
    [(args, env, flags)] = started
    assert args == [str(exe)]
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    # DETACHED_PROCESS (0x8) and CREATE_NEW_PROCESS_GROUP (0x200) on Windows; neither exists elsewhere.
    assert flags == (0x208 if sys.platform == "win32" else 0)


def test_the_next_start_removes_the_replaced_exe(exe: Path) -> None:
    old = exe.parent / "Commander.exe.old"
    old.write_bytes(OLD_EXE)
    remove_old_exe(exe)
    assert not old.exists()
    remove_old_exe(exe)
    remove_old_exe(None)


def test_the_checksum_is_read_from_sha256sum_lines_of_either_mode() -> None:
    digest = hashlib.sha256(NEW_EXE).hexdigest()
    assert expected_sha256(f"{'0' * 64}  other.exe\n{digest}  Commander.exe\n", "Commander.exe") == digest
    assert expected_sha256(f"{digest.upper()} *Commander.exe", "Commander.exe") == digest
    with pytest.raises(UpdateError, match=r"no checksum for Commander\.exe; nothing was changed"):
        expected_sha256("not a sums file", "Commander.exe")


def test_a_redirect_without_a_target_fails_and_a_plain_answer_is_no_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302) if request.url.path == "/moved" else httpx.Response(200, content=b"x")

    client = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(HttpStatusError):
        client.redirect_location("https://github.com/moved")
    assert client.redirect_location("https://github.com/here") is None


# --- the bridge


def make_desk(settings: DeskSettings, found: Updater, closed: list[bool]) -> Desk:
    return Desk(
        settings,
        client_factory=FakeArchidekt().client,
        token_store=MemoryTokenStore(),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
        updater=found,
        quit_app=lambda: closed.append(True),
    )


def test_the_page_is_told_of_a_newer_version_and_whether_the_app_can_install_it(
    settings: DeskSettings, exe: Path
) -> None:
    from_exe = make_desk(settings, updater(FakeGitHub(), "1.3.0", exe)[0], [])
    assert from_exe.update() == {
        "current": __version__,
        "latest": "v1.3.1",
        "page": "https://github.com/gravenuance/Commander/releases/tag/v1.3.1",
        "canInstall": True,
    }
    from_source = make_desk(settings, updater(FakeGitHub(), "1.3.0")[0], [])
    assert from_source.update()["canInstall"] is False
    current = make_desk(settings, updater(FakeGitHub(), "1.3.1")[0], [])
    assert current.update() == {"current": __version__, "latest": None}


def test_installing_from_the_page_restarts_and_closes_the_app_or_says_why_not(
    settings: DeskSettings, exe: Path
) -> None:
    closed: list[bool] = []
    found, restarted = updater(FakeGitHub(), "1.3.0", exe)
    assert make_desk(settings, found, closed).install_update() == {"ok": True, "restarted": True}
    assert (restarted, closed) == ([exe], [True])
    bad = FakeGitHub()
    bad.exe = b"MZ tampered"
    failing, _ = updater(bad, "1.3.0", exe)
    closed.clear()
    answer = make_desk(settings, failing, closed).install_update()
    assert answer == {"error": "The download didn't match its checksum; nothing was changed."}
    assert closed == []


def test_an_installed_update_closes_the_app_even_when_the_new_exe_did_not_start(
    settings: DeskSettings, exe: Path
) -> None:
    def refuse(_: Path) -> None:
        raise OSError("blocked")

    closed: list[bool] = []
    found = Updater(FakeGitHub().client, "1.3.0", exe, restart_exe=refuse)
    assert make_desk(settings, found, closed).install_update() == {"ok": True, "restarted": False}
    assert closed == [True]


def test_no_update_while_a_sync_runs(settings: DeskSettings, exe: Path) -> None:
    closed: list[bool] = []
    found, restarted = updater(FakeGitHub(), "1.3.0", exe)
    pending: list[object] = []
    desk = Desk(
        settings,
        client_factory=FakeArchidekt().client,
        token_store=MemoryTokenStore(),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
        # The sync is started and held, so it runs until the test ends.
        background=pending.append,
        updater=found,
        quit_app=lambda: closed.append(True),
    )
    assert "error" not in desk.sync()
    assert desk.install_update() == {"error": "Wait for the sync to finish, then update."}
    assert (restarted, closed) == ([], [])
    assert exe.read_bytes() == OLD_EXE
