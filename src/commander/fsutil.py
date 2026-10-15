import os
import tempfile
from pathlib import Path


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
