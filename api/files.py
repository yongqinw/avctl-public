"""Directory listing, confined to a single root.

The one thing that actually matters here is containment. A path arriving from
outside is hostile until proven otherwise, and "list files" is exactly the
feature that turns into "read anything on the router" if the check is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import settings


class OutsideRoot(Exception):
    """A requested path resolved to somewhere outside ROOT."""


@dataclass(frozen=True)
class Entry:
    name: str
    path: str  # relative to ROOT, POSIX style, "" for ROOT itself
    is_dir: bool
    size: int | None
    size_h: str  # "4.2K" -- formatted here so the screen has nothing to decide
    modified: str | None
    note: str | None = None  # why we could not stat it, when we could not


def safe_resolve(relative: str) -> Path:
    """Resolve a caller-supplied relative path inside ROOT, or refuse.

    resolve() collapses '..' *and* follows symlinks, so testing containment
    afterwards catches both traversal and a symlink pointing out of the tree.
    Testing before resolving would catch neither.
    """
    candidate = (settings.ROOT / relative.lstrip("/")).resolve()
    if candidate != settings.ROOT and not candidate.is_relative_to(settings.ROOT):
        raise OutsideRoot(relative)
    return candidate


def relative_to_root(path: Path) -> str:
    return "" if path == settings.ROOT else path.relative_to(settings.ROOT).as_posix()


def _stamp(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def _describe(child: Path) -> Entry:
    # lstat, not stat: a broken or looping symlink should be reported as an
    # entry rather than blowing up the whole listing.
    try:
        info = child.lstat()
        is_link = child.is_symlink()
        is_dir = child.is_dir()  # follows the link, which is what a user means
        size = None if is_dir else info.st_size
        return Entry(
            name=child.name,
            path=relative_to_root(child),
            is_dir=is_dir,
            size=size,
            size_h=human_size(size),
            modified=_stamp(info.st_mtime),
            note="symlink" if is_link else None,
        )
    except OSError as exc:
        return Entry(
            name=child.name,
            path=relative_to_root(child),
            is_dir=False,
            size=None,
            size_h="",
            modified=None,
            note=exc.strerror or "unreadable",
        )


def listing(relative: str = "") -> tuple[str, list[Entry]]:
    """Everything in one directory, directories first then name.

    Hidden files are included -- the ask was to list all files, and on a Mac
    mini the dotfiles are usually the interesting ones.
    """
    target = safe_resolve(relative)
    if not target.exists():
        raise FileNotFoundError(relative)
    if not target.is_dir():
        raise NotADirectoryError(relative)

    entries = [_describe(child) for child in target.iterdir()]
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return relative_to_root(target), entries


def human_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return ""
