"""New versions of Commander.exe from the GitHub Releases of its repository (ADR 0059): the check at start-up,
and on the user's click the download, its checksum, the swap of the exe and the restart.

GitHub's API answers 60 requests an hour without a sign-in, far more than one check per start needs. Release
files redirect once from github.com to GitHub's file host; the client follows no redirect itself, so each
target is checked before it is asked for.
"""

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from commander.fsutil import new_temp_path
from commander.jsonshape import ShapeError, as_object, req_bool, req_list, req_str
from commander.net import Fetcher, NetworkError

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

REPOSITORY = "gravenuance/Commander"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"
# Where github.com sends a release file's download (checked 2026-10-30).
FILE_HOSTS = frozenset({"release-assets.githubusercontent.com", "objects.githubusercontent.com"})
EXE_NAME = "Commander.exe"
SUMS_NAME = "SHA256SUMS.txt"
MAX_EXE_BYTES = 200 * 1024 * 1024
MAX_SUMS_BYTES = 64 * 1024
# The replaced exe keeps running under this name until the app restarts; the next start removes it.
OLD_SUFFIX = ".old"
PART_SUFFIX = ".part"
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
# A line of `sha256sum`: the hex digest, a space, and a space (text) or * (binary) before the name.
_SUMS_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")

type Version = tuple[int, int, int]


class UpdateError(Exception):
    """An update that could not be checked for, downloaded or put in place; the running exe is unchanged."""


@dataclass(slots=True, frozen=True)
class Release:
    version: Version
    # The tag, "v1.4.0".
    tag: str
    # The release's page on GitHub, with its notes.
    page: str
    exe_url: str
    sums_url: str


def parse_version(text: str) -> Version | None:
    """1.4.0 or v1.4.0 as numbers; None for anything else, such as a pre-release tag."""
    found = _VERSION.fullmatch(text.strip())
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def parse_release(raw: object) -> Release | None:
    """GitHub's answer for the latest release; None, logged, for one that offers no exe of this repository to
    update to (a draft, a pre-release, an odd tag, a missing file). A malformed answer raises ShapeError."""
    obj = as_object(raw, "release")
    tag = req_str(obj, "tag_name", "release")
    version = parse_version(tag)
    if version is None or req_bool(obj, "draft", "release") or req_bool(obj, "prerelease", "release"):
        log.info("the latest release %s is no version to update to", tag)
        return None
    files: dict[str, str] = {}
    for index, item in enumerate(req_list(obj, "assets", "release")):
        where = f"release.assets[{index}]"
        asset = as_object(item, where)
        files[req_str(asset, "name", where)] = req_str(asset, "browser_download_url", where)
    prefix = f"{DOWNLOAD_PREFIX}{tag}/"
    urls = {name: files.get(name, "") for name in (EXE_NAME, SUMS_NAME)}
    if not all(url == f"{prefix}{name}" for name, url in urls.items()):
        log.warning("release %s lacks %s or %s under %s; it is not offered", tag, EXE_NAME, SUMS_NAME, prefix)
        return None
    page = req_str(obj, "html_url", "release")
    if not page.startswith(f"https://github.com/{REPOSITORY}/releases/"):
        raise ShapeError(f"release.html_url: not a release page of {REPOSITORY}: {page!r}")
    return Release(version, tag, page, urls[EXE_NAME], urls[SUMS_NAME])


def latest_release(fetcher: Fetcher) -> Release | None:
    """The latest published release, or None when there is none to update to."""
    try:
        return parse_release(fetcher.get_json(LATEST_URL, headers=API_HEADERS))
    except ShapeError as error:
        raise UpdateError(f"unexpected release answer from GitHub: {error}") from error


def running_exe() -> Path | None:
    """The exe the app runs as; None when it runs from source, which updates through git instead."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def old_exe(exe: Path) -> Path:
    return exe.with_name(exe.name + OLD_SUFFIX)


def remove_old_exe(exe: Path | None) -> None:
    """Remove the exe an update replaced, once the app runs as the new one."""
    if exe is None:
        return
    try:
        old_exe(exe).unlink(missing_ok=True)
    except OSError as error:
        # Still running, if the restart came quick; the next start tries again.
        log.info("the replaced exe %s is not removed yet: %s", old_exe(exe), error)


def install(fetcher: Fetcher, release: Release, exe: Path) -> None:
    """Download the release's exe next to the running one, check it against the release's SHA256SUMS.txt
    and put it in the running exe's place, which goes aside as Commander.exe.old (Windows lets a running exe
    be renamed, not overwritten). Any failure leaves the running exe where it was, and raises UpdateError with
    a short text for the page; the details go to the log."""
    folder = exe.parent
    temps: list[Path] = []
    try:
        sums = new_temp_path(folder, SUMS_NAME, PART_SUFFIX)
        temps.append(sums)
        part = new_temp_path(folder, EXE_NAME, PART_SUFFIX)
        temps.append(part)
        _download(fetcher, release.sums_url, sums, MAX_SUMS_BYTES)
        expected = expected_sha256(sums.read_text(encoding="utf-8", errors="replace"), EXE_NAME)
        _download(fetcher, release.exe_url, part, MAX_EXE_BYTES)
        if file_sha256(part) != expected:
            log.warning("the downloaded %s of %s does not match its checksum", EXE_NAME, release.tag)
            raise UpdateError("The download didn't match its checksum; nothing was changed.")
        _swap(part, exe)
    except NetworkError as error:
        log.warning("the update to %s could not be downloaded: %s", release.tag, error)
        raise UpdateError("Couldn't download the update; check the connection and try again.") from error
    except OSError as error:
        log.warning("the update to %s could not be put in place at %s: %s", release.tag, exe, error)
        raise UpdateError(f"Couldn't replace {EXE_NAME}; is its folder writable?") from error
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
    log.info("updated %s to %s", exe, release.tag)


def restart(exe: Path) -> None:
    """Start exe on its own, for the app to quit after. The reset tells PyInstaller's start of the new exe
    not to take this one's unpacked files for its own."""
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe)], env=env, creationflags=flags, close_fds=True)  # noqa: S603 — our own exe


def expected_sha256(sums: str, name: str) -> str:
    """The checksum SHA256SUMS.txt gives the file, as `sha256sum` writes it ("<hex>  <name>")."""
    for line in sums.splitlines():
        found = _SUMS_LINE.fullmatch(line.strip())
        if found and found[2] == name:
            return found[1].lower()
    log.warning("%s of the release has no checksum for %s", SUMS_NAME, name)
    raise UpdateError(f"The release has no checksum for {name}; nothing was changed.")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(fetcher: Fetcher, url: str, dest: Path, max_bytes: int) -> None:
    """A release file, through the one redirect to GitHub's file host, each address checked first."""
    unexpected = UpdateError("The download came from an unexpected address; nothing was changed.")
    if not url.startswith(DOWNLOAD_PREFIX):
        log.warning("%s is not a release file of %s", url, REPOSITORY)
        raise unexpected
    target = fetcher.redirect_location(url) or url
    if target != url:
        parts = urlsplit(target)
        host = (parts.hostname or "").rstrip(".").lower()
        if parts.scheme != "https" or host not in FILE_HOSTS:
            log.warning("the release file %s redirects to %s://%s", url, parts.scheme, parts.hostname)
            raise unexpected
    fetcher.download(target, dest, max_bytes=max_bytes)


def _swap(new: Path, exe: Path) -> None:
    old = old_exe(exe)
    old.unlink(missing_ok=True)
    exe.rename(old)
    try:
        new.rename(exe)
    except OSError:
        old.rename(exe)
        raise


class Updater:
    """The app's view of updates: the start-up check, made once per run, and the install on request."""

    def __init__(
        self,
        client_factory: Callable[[], "httpx.Client"],
        current: str,
        exe: Path | None,
        *,
        restart_exe: Callable[[Path], None] = restart,
    ) -> None:
        self._client_factory = client_factory
        self._current = parse_version(current)
        self.exe = exe
        self._restart_exe = restart_exe
        self._lock = threading.Lock()
        self._checked = False
        self._release: Release | None = None
        # Once the new exe is in place this run must end: PyInstaller reads each module it has not loaded
        # yet from the exe's path, which now holds the new version.
        self._installing = threading.Lock()
        self.installed = False

    def newer_release(self) -> Release | None:
        """The latest release when it is newer than this app, asked once per run; a failed check is logged
        and offers nothing, and the next start asks again."""
        with self._lock:
            if not self._checked:
                self._checked = True
                self._release = self._fetch()
            return self._release

    def install_and_restart(self) -> bool:
        """Put the newer release in place, once a run, and start it; whether it started. The caller closes
        the app either way once this returns, since this run cannot go on from the replaced exe."""
        release = self.newer_release()
        if release is None:
            raise UpdateError("No newer version to install.")
        if self.exe is None:
            raise UpdateError("Running from source; update it with git.")
        if not self._installing.acquire(blocking=False):
            raise UpdateError("The update is already under way.")
        try:
            if self.installed:
                raise UpdateError("The update is in place; start Commander again.")
            with self._client_factory() as http:
                install(Fetcher(http), release, self.exe)
            self.installed = True
        finally:
            self._installing.release()
        try:
            self._restart_exe(self.exe)
        except OSError:
            log.exception("the updated %s did not start; it starts next time Commander is opened", self.exe)
            return False
        return True

    def _fetch(self) -> Release | None:
        try:
            with self._client_factory() as http:
                release = latest_release(Fetcher(http))
        except (NetworkError, UpdateError) as error:
            log.warning("could not check for a newer version: %s", error)
            return None
        if release is None or self._current is None or release.version <= self._current:
            return None
        log.info("version %s is out", release.tag)
        return release
