#!/usr/bin/env python3
"""Copy explicitly selected Git blobs into a public checkout, without importing history."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

try:
    from .check_public_snapshot import SnapshotError, git, inspect_blobs, read_denylist, read_snapshot, report
except ImportError:
    from check_public_snapshot import SnapshotError, git, inspect_blobs, read_denylist, read_snapshot, report


PUBLIC_FILES = {
    "LICENSE", "README.md", "AGENTS.md", ".github/workflows/tests.yml",
    "docs/friend-setup.md", "docs/public-contributions.md",
    "scripts/check_public_snapshot.py", "scripts/export_public_snapshot.py", "tests/test_public_export.py",
}
PUBLIC_PREFIXES = ("docs/images/friend-setup/",)


def export_snapshot(source: Path, destination: Path, paths: list[str], *, ref: str | None = None,
                    deny_terms: tuple[str, ...] = (), allow_binary: bool = False, write: bool = False) -> list[str]:
    if not paths:
        raise SnapshotError("Select the specific tracked files or directories to export.")
    if destination.is_symlink():
        raise SnapshotError("The public checkout must not be a symlink.")
    destination = destination.resolve(strict=True)
    top = Path(git(destination, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != destination:
        raise SnapshotError("The destination must be the root of the public Git checkout.")
    source_top = Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if destination == source_top or destination.is_relative_to(source_top) or source_top.is_relative_to(destination):
        raise SnapshotError("Use separate source and public checkouts, without nesting either checkout.")
    blobs = read_snapshot(source, paths, ref)
    findings = inspect_blobs(blobs, deny_terms, allow_binary)
    if findings:
        report(findings)
        raise SnapshotError("Export stopped before writing: review and sanitize the selected source snapshot.")
    # Validate every destination before creating directories or writing any content.
    for blob in blobs:
        if blob.path.casefold() in {path.casefold() for path in PUBLIC_FILES} or blob.path.casefold().startswith(PUBLIC_PREFIXES):
            raise SnapshotError(f"{blob.path}: public-specific file; update it manually instead of exporting.")
        target = destination / blob.path
        current = target
        while current != destination:
            if current.is_symlink():
                raise SnapshotError(f"{blob.path}: destination includes a symlink.")
            if current.exists() and ((current == target and not current.is_file()) or (current != target and not current.is_dir())):
                raise SnapshotError(f"{blob.path}: destination has an incompatible file or directory.")
            current = current.parent
    if write:
        for blob in blobs:
            target = destination / blob.path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".public-export-", delete=False) as stream:
                    temporary = stream.name
                    stream.write(blob.data)
                    os.fchmod(stream.fileno(), 0o755 if blob.mode == "100755" else 0o644)
                os.replace(temporary, target)
                temporary = None
            finally:
                if temporary is not None:
                    os.unlink(temporary)
    return [blob.path for blob in blobs]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Local source Git checkout")
    parser.add_argument("--destination", type=Path, default=Path.cwd(), help="Separate public Git checkout root")
    parser.add_argument("--ref", help="Read a committed ref; otherwise read the source's full staged index")
    parser.add_argument("--denylist", type=Path, help="Local personal-string denylist outside the public checkout")
    parser.add_argument("--allow-binary", action="store_true", help="Accept binary files ONLY after separate manual review")
    parser.add_argument("--write", action="store_true", help="Write after validation; default is a dry run")
    parser.add_argument("paths", nargs="+", help="Literal tracked files or directories, without glob/pathspec syntax")
    args = parser.parse_args(argv)
    try:
        selected = export_snapshot(
            args.source, args.destination, args.paths, ref=args.ref,
            deny_terms=read_denylist(args.denylist, args.destination), allow_binary=args.allow_binary, write=args.write,
        )
        for path in selected:
            print(path)
        action = "Copied" if args.write else "Would copy"
        print(f"{action} {len(selected)} files. No deletions, Git configuration changes, commits, or pushes were made.")
        return 0
    except (SnapshotError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
