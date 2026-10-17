import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# Windows refuses to replace a file another process has open; they let go within moments.
REPLACE_ATTEMPTS = 5
REPLACE_FIRST_DELAY_SECONDS = 0.1


def write_text_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same folder and rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def new_temp_path(folder: Path, name: str, suffix: str) -> Path:
    """An empty file of its own in folder, so two processes working on `name` never share a temp file."""
    fd, temp_name = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=suffix)
    os.close(fd)
    return Path(temp_name)


def replace_with_retry(source: Path, dest: Path, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Move source over dest, waiting out a PermissionError from another process holding dest open.

    The delay doubles from 0.1s over REPLACE_ATTEMPTS tries; the last PermissionError is raised.
    """
    delay = REPLACE_FIRST_DELAY_SECONDS
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            source.replace(dest)
        except PermissionError as error:
            if attempt == REPLACE_ATTEMPTS:
                raise
            log.info("%s is in use (%s); trying again in %.1fs", dest, error, delay)
            sleep(delay)
            delay *= 2
        else:
            return
