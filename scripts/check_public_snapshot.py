#!/usr/bin/env python3
"""Check selected Git blobs for common accidental disclosures, without a network call."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MAX_BLOB_BYTES = 10 * 1024 * 1024
EXAMPLE_WORDS = ("example", "placeholder", "dummy", "synthetic", "test-", "test_", "your_")
SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("provider token", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b")),
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r'''(?im)\b(?:[\w-]*(?:api[_-]?key|secret|password|token)|authorization)["']?\s*[:=]\s*["']([^"'\r\n]{16,})["']'''
)
HOME_PATH = re.compile(r"/(?:Users|home)/([^/\s\"'<>]+)")
TAILSCALE_HOST = re.compile(r"\b([a-z0-9-]+)\.([a-z0-9-]+)\.ts\.net\b", re.I)
EXAMPLE_HOMES = {"user", "username", "example", "your-user", "yourname", "runner"}
EXAMPLE_HOSTS = {"core", "broker", "host", "device", "example", "example-core", "example-broker", "your-core", "your-mac"}
EXAMPLE_TAILNETS = {"tailnet", "example", "example-tailnet", "tail-example", "your-tailnet"}


class SnapshotError(ValueError):
    """Unsafe input or an unreadable Git snapshot."""


@dataclass(frozen=True)
class Blob:
    path: str
    mode: str
    data: bytes


@dataclass(frozen=True)
class Finding:
    path: str
    category: str


def safe_path(value: str) -> str:
    """Accept only an unambiguous repository-relative POSIX path."""
    parts = value.split("/")
    if (
        not value
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} or part.casefold() == ".git" or part.endswith((".", " ")) for part in parts)
        or PurePosixPath(value).is_absolute()
    ):
        raise SnapshotError("Unsafe repository-relative path.")
    return value


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise SnapshotError("Cannot read Git snapshot; check repository, revision, and index conflicts.")
    return result.stdout


def read_snapshot(repo: Path, paths: list[str], ref: str | None = None) -> list[Blob]:
    """Read the full index or a tree, then select literal files/directories. Never open worktree files."""
    selectors = [safe_path(path) for path in paths]
    repo = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip())
    if ref is None:
        listing = git(repo, "ls-files", "--stage", "-z")
    else:
        tree = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{tree}").strip().decode("ascii")
        listing = git(repo, "ls-tree", "-r", "-z", tree)
    records: list[tuple[str, str, str]] = []
    matched: set[str] = set()
    names: set[str] = set()
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            path = raw_path.decode("utf-8")
            mode, middle, last = metadata.decode("ascii").split()
        except (ValueError, UnicodeError) as exc:
            raise SnapshotError("Unreadable Git path or metadata.") from exc
        selected = [item for item in selectors if path == item or path.startswith(item + "/")]
        if selectors and not selected:
            continue
        matched.update(selected)
        safe_path(path)
        if mode not in {"100644", "100755"}:
            raise SnapshotError(f"{path}: symlinks, submodules, and special files are not exportable.")
        if ref is None and last != "0":
            raise SnapshotError(f"{path}: unresolved index conflict.")
        if path.casefold() in names:
            raise SnapshotError(f"{path}: duplicate or case-colliding path.")
        names.add(path.casefold())
        oid = middle if ref is None else last
        if not re.fullmatch(r"[a-f0-9]{40,64}", oid):
            raise SnapshotError("Invalid Git object identifier.")
        records.append((path, mode, oid))
    if set(selectors) != matched:
        raise SnapshotError("A selected path has no tracked files in this snapshot.")
    blobs = []
    for path, mode, oid in records:
        if int(git(repo, "cat-file", "-s", oid)) > MAX_BLOB_BYTES:
            raise SnapshotError(f"{path}: blob exceeds the review size limit.")
        blobs.append(Blob(path, mode, git(repo, "cat-file", "blob", oid)))
    return blobs


def unsafe_file(path: str) -> bool:
    parts = PurePosixPath(path.casefold()).parts
    name = parts[-1]
    example = any(marker in name for marker in (".example", ".sample", ".template"))
    return (
        any(part in {".ssh", ".aws", ".avctl", "__pycache__", "node_modules"} for part in parts)
        or name in {".ds_store", "id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc", "credentials"}
        or name.endswith((".p8", ".p12", ".pfx", ".key", ".keychain", ".keychain-db", ".mobileprovision", ".sqlite", ".sqlite3", ".db", ".log"))
        or (not example and (name == ".env" or name.startswith(".env.") or name in {"config.local.yaml", "config.local.json", "secrets.json", "secrets.yaml", "token"}))
    )


def inspect_blobs(blobs: list[Blob], deny_terms: tuple[str, ...] = (), allow_binary: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for blob in blobs:
        categories: set[str] = set()
        if unsafe_file(blob.path):
            categories.add("credential or runtime file")
        try:
            if b"\0" in blob.data:
                raise UnicodeError()
            content = blob.data.decode("utf-8")
        except UnicodeError:
            content = ""
            if not allow_binary:
                categories.add("binary requires separate manual review")
        for category, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                categories.add(category)
        for match in CREDENTIAL_ASSIGNMENT.finditer(content):
            value = match.group(1).casefold()
            if value.startswith("bearer "):
                value = value.removeprefix("bearer ")
            # Obvious documentation placeholders and code expressions are not credentials.
            if not any(word in value for word in EXAMPLE_WORDS) and not any(char in value for char in "{}<>$()\\ "):
                categories.add("possible embedded credential")
        for match in HOME_PATH.finditer(content):
            if match.group(1).casefold() not in EXAMPLE_HOMES and not any(char in match.group(1) for char in "$({["):
                categories.add("personal home path")
        for match in TAILSCALE_HOST.finditer(content):
            if match.group(1).casefold() not in EXAMPLE_HOSTS or match.group(2).casefold() not in EXAMPLE_TAILNETS:
                categories.add("non-example Tailscale hostname")
        lowered = (blob.path + "\n" + content).casefold()
        if any(term.casefold() in lowered for term in deny_terms):
            categories.add("local privacy denylist match")
        findings.extend(Finding(blob.path, category) for category in sorted(categories))
    return findings


def read_denylist(path: Path | None, public_repo: Path) -> tuple[str, ...]:
    if path is None:
        return ()
    try:
        resolved = path.resolve(strict=True)
        if resolved.is_relative_to(public_repo.resolve()):
            raise SnapshotError("Keep the personal denylist outside the public repository.")
        terms = tuple(line.strip() for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#"))
    except (OSError, UnicodeError) as exc:
        raise SnapshotError("Cannot read the explicitly supplied local denylist.") from exc
    if not terms:
        raise SnapshotError("The supplied denylist is empty.")
    return terms


def report(findings: list[Finding]) -> None:
    for finding in findings:
        print(f"{finding.path}: {finding.category}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Literal tracked files/directories; default is the full snapshot")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--ref", help="Read this committed Git ref instead of the index")
    selection.add_argument("--staged", action="store_true", help="Check the full staged index (the default)")
    parser.add_argument("--denylist", type=Path, help="Explicit local text file outside the public repo, one private string per line")
    parser.add_argument("--allow-binary", action="store_true", help="Accept binary files ONLY after separate manual review")
    args = parser.parse_args(argv)
    try:
        blobs = read_snapshot(args.repo, args.paths, args.ref)
        findings = inspect_blobs(blobs, read_denylist(args.denylist, args.repo), args.allow_binary)
        report(findings)
        if findings:
            return 1
        print(f"Checked {len(blobs)} tracked files; no configured checks matched. Manual review is still required.")
        return 0
    except (SnapshotError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
