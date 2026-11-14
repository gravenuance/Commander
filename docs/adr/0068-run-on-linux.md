# 0068: Run on Linux, with Qt for the window and Windows-only Cardmarket scanning

**Status:** Accepted · **Date:** 2026-11-14

## Context

Commander ran on Windows only: its folders came from `%APPDATA%` and `%LOCALAPPDATA%`, its window from
WebView2 through pywebview and pythonnet, its secrets from Windows Credential Manager, and the Cardmarket
Buy screen from a frameless WebView2 window placed over the page with Win32 calls. The owner wants the
command line and the app to run on Ubuntu too, with Windows unchanged, and the folders to follow each
system's own convention (the user's decision of 2026-11-14).

## Options

Folders:

- **Keep `%APPDATA%` and `%LOCALAPPDATA%` and fall back to `~/.config` and `~/.cache` elsewhere** (the old
  fallback): works, but the folder is named `Commander` and ignores `XDG_*` overrides.
- **XDG base directories on Linux** (`$XDG_CONFIG_HOME/commander`, `$XDG_CACHE_HOME/commander`, each falling
  back to `~/.config` and `~/.cache`): what Linux tools and backup scripts expect. `CMDR_CONFIG` and
  `CMDR_CACHE_DIR` stay the overrides on both systems.
- **A dependency such as platformdirs:** one more package for four lines of logic.

The app's window on Linux:

- **Qt (`pywebview[qt]`: PyQt6 and PyQt6-WebEngine):** pip wheels with Qt inside; the system only needs the
  libraries Chromium's engine loads (nss, alsa, xcb helpers). The PyInstaller build is about 280 MB.
- **GTK (PyGObject and WebKitGTK):** a lighter window, but PyGObject builds from system packages and is not
  installable into a uv environment without them.
- **No window on Linux:** the command line alone.

The Cardmarket scan on Linux: port the frameless owned window (no equivalent of the Win32 cover and of
WebView2's cookie and navigation calls exists in Qt or GTK without a rewrite), or leave it Windows-only.

## Decision

- `config.py` is the one place that builds the folders. Windows keeps `%APPDATA%\Commander` and
  `%LOCALAPPDATA%\Commander` (the old fallback to `~/.config` and `~/.cache` included); elsewhere the XDG
  folders above apply, an empty or relative `XDG_*` value ignored as the specification says. The window's
  profile (`webview`), the logs and every cache stay inside the cache folder. `--help` and the tools print the
  locations of the system they run on.
- The window is Qt on Linux: `pywebview[qt]` is a dependency on Linux only, and `run_app` asks pywebview for
  the backend `window_backend` chooses: the one `PYWEBVIEW_GUI` names, else `qt` on Linux (so GTK is not tried
  first and logged as a failure), else pywebview's own pick (other systems are left to it). The Qt and GTK
  backends build the page bridge with `new Function`, so the page's Content-Security-Policy gains
  `'unsafe-eval'` for scripts when that backend is the one chosen, not by operating system (checked on Qt; GTK
  is assumed to behave the same). The page still loads nothing from outside (`default-src 'none'`), runs only
  the scripts inlined into it and writes text nodes only (never `innerHTML`), so the risk of the keyword is
  small, and the Windows window keeps the stricter policy.
- A window toolkit that will not start (missing system libraries, no display) is reported, not swallowed:
  `WindowError` names the README's library list on Linux, and off Windows the entry point prints it and
  sends a `notify-send` notification when that program exists.
- A new account's deck folder goes under the Documents folder the desktop names on Linux
  (`XDG_DOCUMENTS_DIR`, then `user-dirs.dirs`, then `~/Documents`).
- One module, `osinfo`, answers "is this Windows or Linux" for all of the above.
- The Cardmarket scan stays Windows-only. Off Windows the engine reports why (`BuyEngine.unavailable`) and the
  Buy screen's scan button answers with that message; shipping tables and the rest of the screen still work.
- In-place update stays Windows-only: the release holds `Commander.exe`, and a Linux binary is not published.
  Off Windows the update notice only links to the release page.
- Logins and keys use whatever backend `keyring` finds: Windows Credential Manager, or the Secret Service on
  Linux. A session without one, a locked one or a refused D-Bus call gets a `CredentialError` saying so (the
  errors of `win32ctypes`, `secretstorage` and `jeepney` that keyring passes through are mapped, each imported
  only where installed). Messages name the store of the system, "the system keyring" on other systems.
- pyright checks as Windows (`pythonPlatform`) everywhere, because the Win32 calls have type stubs only there.
  CI runs the checks and tests on `windows-latest` and `ubuntu-latest`.

## Consequences

- Linux users get the command line and the app, but not the Cardmarket Buy scan or self-update; a Linux
  release artifact is not built yet (a 280 MB onefile that still needs system libraries is a poor download,
  and its release job would need an artifact hand-off between runners). Revisit with a GTK or Qt rewrite of
  the window cover if the Buy screen is wanted on Linux.
- The app's Qt window was verified headless (offscreen) in WSL, not on a desktop; look and feel there are
  unchecked.
- `'unsafe-eval'` on Linux is a deliberate loosening, to be dropped if pywebview stops evaluating strings.
- The Linux config folder is lower-case `commander`: a config written by an older Linux run of the program
  under `~/.config/Commander` is not read (that fallback was never documented).
