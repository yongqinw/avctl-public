"""Privacy/export regression tests use temporary repositories and synthetic data only."""

from pathlib import Path
import subprocess

import pytest

from scripts.check_public_snapshot import Blob, SnapshotError, inspect_blobs, main as check_main, read_denylist, read_snapshot, safe_path
from scripts.export_public_snapshot import export_snapshot


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


def make_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    (path / "initial.txt").write_text("Initial public file.\n")
    git(path, "add", "initial.txt")
    git(path, "-c", "user.name=Example Contributor", "-c", "user.email=contributor@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Initial snapshot")
    return path


def tracked(repo, path, text):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repo, "add", "--", path)
    return target


@pytest.fixture
def repos(tmp_path):
    return make_repo(tmp_path / "source"), make_repo(tmp_path / "public")


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a/../b", ".git/config", "a/.GIT/config",
                                    "a//b", "a\\b", "a\nb", "a\tb", "a/./b", "C:drive", ".git /config", "a."])
def test_unsafe_paths_are_rejected(path):
    with pytest.raises(SnapshotError):
        safe_path(path)


def test_snapshot_uses_only_index_blobs_and_excludes_untracked_and_unstaged_files(repos):
    source, destination = repos
    file = tracked(source, "api/example.py", "print('staged version')\n")
    file.write_text("print('unstaged version')\n")
    (source / "api" / "untracked.txt").write_text("Do not copy this untracked file.\n")
    assert export_snapshot(source, destination, ["api"], write=True) == ["api/example.py"]
    assert (destination / "api/example.py").read_text() == "print('staged version')\n"
    assert not (destination / "api/untracked.txt").exists()


def test_ref_reads_committed_tree_instead_of_index(repos):
    source, _ = repos
    tracked(source, "initial.txt", "Uncommitted staged change.\n")
    assert read_snapshot(source, ["initial.txt"], ref="HEAD")[0].data == b"Initial public file.\n"


def test_checker_from_subdirectory_still_checks_the_full_index(repos):
    source, _ = repos
    tracked(source, "api/file.py", "print('fine')\n")
    assert {blob.path for blob in read_snapshot(source / "api", [])} == {"initial.txt", "api/file.py"}


def test_missing_path_and_option_like_ref_fail_closed(repos):
    source, destination = repos
    for paths, ref in [(["missing.py"], None), (["initial.txt"], "--all")]:
        with pytest.raises(SnapshotError):
            export_snapshot(source, destination, paths, ref=ref, write=True)
    assert git(destination, "status", "--porcelain") == b""


def test_source_symlink_is_rejected_without_reading_target(repos, tmp_path):
    source, destination = repos
    # The target does not even exist. Reading it would fail rather than produce a blob.
    (source / "link").symlink_to(tmp_path / "never-open-this")
    git(source, "add", "link")
    with pytest.raises(SnapshotError, match="symlinks"):
        export_snapshot(source, destination, ["link"], write=True)
    assert not (destination / "link").exists()


def test_submodule_is_rejected(repos):
    source, destination = repos
    oid = git(source, "rev-parse", "HEAD").decode().strip()
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{oid},dependency")
    with pytest.raises(SnapshotError, match="submodules"):
        export_snapshot(source, destination, ["dependency"], write=True)


@pytest.mark.parametrize("directory_link", [False, True])
def test_destination_symlink_cannot_write_outside_checkout(repos, tmp_path, directory_link):
    source, destination = repos
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "file.py"
    sentinel.write_text("Keep this unchanged.\n")
    tracked(source, "api/file.py", "print('new')\n")
    if directory_link:
        (destination / "api").symlink_to(outside, target_is_directory=True)
    else:
        (destination / "api").mkdir()
        (destination / "api/file.py").symlink_to(sentinel)
    with pytest.raises(SnapshotError, match="symlink"):
        export_snapshot(source, destination, ["api"], write=True)
    assert sentinel.read_text() == "Keep this unchanged.\n"


def test_privacy_failure_prevents_all_writes_and_never_changes_git_configuration(repos):
    source, destination = repos
    tracked(source, "api/okay.py", "print('fine')\n")
    tracked(source, "api/.env", "SYNTHETIC_CONFIGURATION=yes\n")
    config_before = (destination / ".git/config").read_bytes()
    index_before = (destination / ".git/index").read_bytes()
    with pytest.raises(SnapshotError, match="before writing"):
        export_snapshot(source, destination, ["api"], write=True)
    assert not (destination / "api").exists()
    assert (destination / ".git/config").read_bytes() == config_before
    assert (destination / ".git/index").read_bytes() == index_before


@pytest.mark.parametrize("path", ["README.md", "LICENSE", "AGENTS.md", ".github/workflows/tests.yml", "docs/friend-setup.md", "scripts/check_public_snapshot.py",
                                    "docs/images/friend-setup/new.png", "Readme.MD"])
def test_public_variants_are_protected(repos, path):
    source, destination = repos
    tracked(source, path, "Private variant must not replace the public version.\n")
    with pytest.raises(SnapshotError, match="public-specific"):
        export_snapshot(source, destination, [path], write=True)
    assert git(destination, "status", "--porcelain") == b""


def test_dry_run_is_default_and_writing_preserves_mode_without_importing_history(repos):
    source, destination = repos
    file = tracked(source, "scripts/example.sh", "#!/bin/sh\nexit 0\n")
    file.chmod(0o755)
    git(source, "add", "scripts/example.sh")
    public_head = git(destination, "rev-parse", "HEAD")
    public_count = git(destination, "rev-list", "--count", "HEAD")
    public_config = (destination / ".git/config").read_bytes()
    source_status = git(source, "status", "--porcelain")
    export_snapshot(source, destination, ["scripts/example.sh"])
    assert not (destination / "scripts").exists()
    export_snapshot(source, destination, ["scripts/example.sh"], write=True)
    assert (destination / "scripts/example.sh").stat().st_mode & 0o777 == 0o755
    assert git(destination, "rev-parse", "HEAD") == public_head
    assert git(destination, "rev-list", "--count", "HEAD") == public_count
    assert (destination / ".git/config").read_bytes() == public_config
    assert git(source, "status", "--porcelain") == source_status
    assert git(destination, "diff", "--cached", "--name-only") == b""


def test_source_and_public_checkouts_must_be_separate(repos):
    source, _ = repos
    with pytest.raises(SnapshotError, match="separate"):
        export_snapshot(source, source, ["initial.txt"], write=True)


@pytest.mark.parametrize("data, category", [
    (("-----BEGIN " + "PRIVATE KEY-----\n").encode(), "private key"),
    (("gh" + "p_" + "A" * 36).encode(), "GitHub token"),
    (("api_key = \"" + "A" * 32 + "\"\n").encode(), "possible embedded credential"),
    (("Authorization: \"Bearer " + "A" * 32 + "\"\n").encode(), "possible embedded credential"),
    (("/Users/" + "private-person/project").encode(), "personal home path"),
    (("private-device." + "tailabcdef.ts.net").encode(), "non-example Tailscale hostname"),
])
def test_common_disclosures_are_detected_without_returning_values(data, category):
    findings = inspect_blobs([Blob("example.txt", "100644", data)])
    assert category in {finding.category for finding in findings}
    assert all(finding.path == "example.txt" for finding in findings)


def test_checker_reports_category_and_filename_without_secret_value(repos, capsys):
    source, _ = repos
    synthetic_value = "gh" + "p_" + "Z" * 36
    tracked(source, "api/example.py", synthetic_value)
    assert check_main(["--repo", str(source), "--staged", "api"]) == 1
    output = capsys.readouterr()
    assert synthetic_value not in output.err + output.out
    assert "api/example.py: GitHub token" in output.err


def test_examples_and_variable_names_are_not_credentials():
    content = b'FIREWORKS_API_KEY\napi_key = "your_fireworks_api_key"\nhttps://broker.example-tailnet.ts.net\n/Users/example/project\n'
    assert inspect_blobs([Blob("docs/example.md", "100644", content)]) == []


def test_binary_requires_manual_review_opt_in():
    blob = Blob("docs/image.png", "100644", b"\x89PNG\0\xff")
    assert inspect_blobs([blob])[0].category == "binary requires separate manual review"
    assert inspect_blobs([blob], allow_binary=True) == []


def test_local_denylist_catches_personal_text_and_must_stay_outside_public_repo(repos, tmp_path):
    _, destination = repos
    denylist = tmp_path / "local-privacy-denylist.txt"
    denylist.write_text("# Local review terms\nprivate-person\n")
    terms = read_denylist(denylist, destination)
    assert inspect_blobs([Blob("notes.txt", "100644", b"PRIVATE-PERSON")], terms)[0].category == "local privacy denylist match"
    forbidden = destination / "private-review-terms.txt"
    forbidden.write_text("private-person\n")
    with pytest.raises(SnapshotError, match="outside"):
        read_denylist(forbidden, destination)
