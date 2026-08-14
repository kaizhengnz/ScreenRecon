"""Local archive (design doc 5.6).

Files are named ``YYYYMMDD_HHMMSS.jpg`` with a matching ``.txt`` (UTF-8). The
directory is created if missing (``~`` is expanded). Write failures print a
warning and never interrupt the watch loop.

Captures can contain anything that was on screen, so both the archive directory
and the files in it are created owner-only on POSIX platforms.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import ui

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def restrict(path: Path, mode: int) -> None:
    """Apply owner-only permissions on POSIX. A no-op on Windows, which has no mode bits."""
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        path.chmod(mode)


def make_private_dir(path: Path) -> Path:
    """Create a directory (with parents) that only the owner can read or enter.

    Permissions are only ever tightened on a directory this call creates. A
    pre-existing directory belongs to the user's wider environment — `save_dir`
    may be an existing shared folder, and the parent of a ``--config`` path can
    be ``$HOME`` or ``/etc`` — and silently chmod'ing those would break
    everything else that reads them.
    """
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if created:
        # mkdir's mode is masked by umask, so tighten the leaf explicitly.
        restrict(path, PRIVATE_DIR_MODE)
    return path


REPLACE_ATTEMPTS = 4
REPLACE_BACKOFF_SECONDS = 0.05


def _replace_with_retry(temp_path: Path, path: Path) -> None:
    """os.replace, retrying the transient Windows sharing violation.

    os.replace needs delete access on the destination, and Windows denies it
    while any process holds an ordinary read handle — an on-access virus
    scanner, the search indexer, a sync agent, or an editor with the file open.
    These clear in milliseconds, so a short retry avoids failing a wizard run
    that has already collected the user's credentials.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_BACKOFF_SECONDS * (attempt + 1))


def write_private_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, owner-only from the moment of creation.

    Writing via a temporary file in the same directory means the destination is
    never observable in a half-written state, the content is never briefly
    readable at the process umask, and a symlink planted at ``path`` is replaced
    rather than followed.
    """
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".screenrecon-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            # Without this the rename can be committed while the data blocks are
            # not, leaving exactly the truncated file this function exists to
            # prevent.
            os.fsync(stream.fileno())
        restrict(temp_path, PRIVATE_FILE_MODE)
        _replace_with_retry(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def write_private_text(path: Path, text: str) -> None:
    """UTF-8 counterpart of :func:`write_private_bytes`."""
    write_private_bytes(path, text.encode("utf-8"))


def normalise_dir(save_dir: str | os.PathLike[str]) -> Path:
    """Expand ``%VARS%`` and ``~``, trim stray whitespace, and make the path absolute.

    Windows silently drops a trailing space when creating a directory but keeps it
    in the path object, which would make every later write fail; and ``%APPDATA%``
    is what a Windows user naturally types, so expand it rather than creating a
    directory literally named ``%APPDATA%``.
    """
    text = os.path.expandvars(str(save_dir)).strip()
    return Path(text).expanduser().resolve()


def resolve_dir(save_dir: str | os.PathLike[str]) -> Path:
    """Normalise the path, create the directory owner-only, and return it.

    Raises OSError, or RuntimeError when ``~`` cannot be resolved because the
    platform reports no home directory.
    """
    return make_private_dir(normalise_dir(save_dir))


def new_stem(directory: Path, now: datetime | None = None) -> str:
    """Return a timestamp filename stem that does not collide with existing files.

    Consecutive triggers are always separated by the mouse leaving the region, so
    same-second collisions are effectively impossible there — but the ``ask``
    subcommand can be run twice within one second, so back off anyway. The
    collision check covers ``.jpg`` (current), ``.png`` (pre-0.1.5 archives the
    user may still have around) and ``.txt`` so a new run never overwrites an
    old triplet even when the format changed under it.
    """
    base = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    stem = base
    counter = 1
    while any(
        (directory / f"{stem}{ext}").exists() for ext in (".jpg", ".png", ".txt")
    ):
        stem = f"{base}_{counter}"
        counter += 1
    return stem


def save_jpeg(directory: Path, stem: str, jpeg_bytes: bytes) -> Path | None:
    """Write the JPEG. On failure, warn and return None."""
    target = directory / f"{stem}.jpg"
    try:
        write_private_bytes(target, jpeg_bytes)
    except OSError as exc:
        ui.warn(f"Could not save the screenshot ({target}): {exc.strerror or exc}")
        return None
    return target


def save_txt(directory: Path, stem: str, text: str) -> Path | None:
    """Write the UTF-8 text file. On failure, warn and return None."""
    target = directory / f"{stem}.txt"
    try:
        write_private_text(target, text)
    except OSError as exc:
        ui.warn(f"Could not save the recognised text ({target}): {exc.strerror or exc}")
        return None
    return target
