# 0059: Update the app from GitHub Releases

**Status:** Accepted · **Date:** 2026-10-30

## Context

Each `v*` tag builds `Commander.exe` in GitHub Actions and publishes it on the repository's Releases page with
a `SHA256SUMS.txt` and a build attestation (`release.yml`). The owner runs the exe, and learns of a new
version only by looking there. They want the app to say when a newer version is out and, on a click,
download and install it.

GitHub's REST API answers `GET /repos/{owner}/{repo}/releases/latest` without a sign-in, 60 requests an hour
per address, with the release's tag, page and files (checked 2026-10-30). A release file's address on
`github.com` redirects once to `release-assets.githubusercontent.com`. The app's HTTP client follows no
redirect (`net.make_client`). Windows does not let a running exe be overwritten or deleted, but does let it
be renamed.

## Options

1. **Only link to the release page.** No download code at all, but the owner still downloads and replaces
   the exe by hand.
2. **Download and swap the exe in the app**, checked against the release's checksum. One click; the app has
   to replace itself safely.
3. **A separate updater exe or an installer framework.** It handles locked files and rollback, at the cost
   of a second artifact to build, sign and ship.

## Decision

Option 2, decided with the owner on 2026-10-30: check at start-up, download only on the click, and trust
HTTPS plus the checksum (verifying the attestation would need GitHub's `gh` tool on the owner's machine).

- **The check** (`updates.Updater.newer_release`): when the page loads, the app asks for the latest release
  once per run. A release counts when its tag is a plain version (`v1.4.0`; a pre-release tag, a draft or a
  pre-release is none), it carries `Commander.exe` and `SHA256SUMS.txt` at
  `github.com/gravenuance/Commander/releases/download/<tag>/`, and its version is above the running one. The
  answer goes through `jsonshape` validation. A failed check is logged and shows nothing; the next start
  asks again.
- **The notice**: at the foot of the deck list, "Version 1.4.0 is out." with a *What's new* link to the
  release page and, in the exe only, an *Update and restart* button. Run from source, there is no button.
- **The download**: each release file is asked of `github.com`, its one redirect is followed only to an
  `https` address on `release-assets.githubusercontent.com` or `objects.githubusercontent.com` (the
  hostname compared exactly), and the download is capped (200 MB for the exe). Both files go to temp files
  in the exe's own folder, so the swap is a rename on one volume, even through the owner's
  `D:\Tools\Commander` junction.
- **The check of the file**: the exe's SHA-256 must equal its line in the release's `SHA256SUMS.txt`, or
  nothing changes.
- **The swap**: the running exe is renamed to `Commander.exe.old` and the new one takes its name; if that
  fails, the running exe gets its name back. The app then starts the new exe on its own (with
  `PYINSTALLER_RESET_ENVIRONMENT=1`, so PyInstaller's start of the new exe does not take the old one's
  unpacked files for its own) and closes its window. The next start removes `Commander.exe.old`.
- **Once the swap is done, this run ends.** PyInstaller reads each module it has not loaded yet from the
  exe's path, which now holds the new version, so the old run cannot safely go on: the window closes even
  when the new exe fails to start (logged; it starts the next time Commander is opened), and a second
  install in the same run is refused. The button stays busy until the window closes.
- **Not during a sync**: closing the window would cut the sync off mid-write, so the update asks to wait.
- **Messages**: a failure shows a short text with what to do ("Couldn't download the update; check the
  connection and try again.", "Couldn't replace Commander.exe; is its folder writable?"); the addresses,
  paths and errors behind it go to the log.

## Consequences

- One GitHub request per app start; downloads only on the owner's click.
- An update depends on the release workflow's file names and checksum file; changing either in `release.yml`
  means changing `updates.py` too.
- A virus scanner holding the new file, or a folder the app cannot write to, ends the update with a message
  and the running exe untouched.
- The checksum guards against a broken or swapped download, not against a compromised repository or
  release workflow: both files come from the same release. The attestation would cover that; revisit if the
  app is ever shared beyond the owner.
- The swap and restart are only proved on the owner's machine; the tests cover the check, the download and
  its checks, and the swap in a temp folder.
