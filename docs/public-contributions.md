# Updating the public source

This repository has its own public history. Maintainers can bring over selected changes from a separate private checkout without publishing that checkout's commits, branches, tags, Git configuration, or untracked files. Contributions are licensed under GPLv3; see [LICENSE](../LICENSE).

## 1. Start from public main

Use a separate checkout of this public repository. Do not add the private repository as a remote, merge its branches, or push its tags into this repository.

```sh
git clone https://github.com/yongqinw/avctl-public.git
cd avctl-public
git switch main
git pull --ff-only origin main
git switch -c update/music-fix
```

For an existing public checkout, begin with a clean working tree, fetch public `main`, and create your branch from it. Keep the private checkout elsewhere; neither checkout should be inside the other.

## 2. Select and review the source files

Inspect the private change locally, then choose specific files or directories to export. Prefer a small change with its relevant tests. The helper reads raw Git blobs: `--ref HEAD` reads the committed source version; omitting `--ref` reads the staged source index. It never reads untracked files or unstaged edits.

The following is a dry run. Replace the example source path and select the actual files for your change:

```sh
python3 scripts/export_public_snapshot.py \
  --source /path/to/private-checkout \
  --destination . \
  --ref HEAD \
  api/musiclink.py tests/test_music_queue.py
```

If a selected file does not exist in that Git snapshot, the export stops. Directory arguments select tracked descendants only. There are no glob expressions or automatic file deletions. Remove obsolete public files manually after reviewing them.

The exporter rejects paths that escape the repository, symlinks, submodules, known credential/runtime file names, common token formats, private-key blocks, personal home paths, and non-example Tailscale addresses. It checks every selected file and destination before writing any files. Validation failures leave the public checkout unchanged. A later filesystem error can leave some already validated files copied, so always review the resulting diff.

Keep known personal names, private account identifiers, old hostnames, and other project-specific strings in a local UTF-8 denylist **outside** the public checkout, one string per line. Comments begin with `#`. Use privacy review terms, not actual passwords or API keys. Add `--denylist /path/to/local-privacy-denylist.txt` to the export and checker commands. The tool reports only the matching filename and category, without printing the matched value.

If the export stops, sanitize a separate local source branch and review it before trying again, or manually copy and sanitize just the needed code in the public branch. Do not remove a privacy check merely to make the export pass. The helper deliberately does not guess how to replace private data.

## 3. Copy the reviewed snapshot

Repeat the accepted dry-run command with `--write`:

```sh
python3 scripts/export_public_snapshot.py \
  --source /path/to/private-checkout \
  --destination . \
  --ref HEAD \
  --write \
  api/musiclink.py tests/test_music_queue.py
```

The helper copies file contents and executable bits. It does not stage files, change Git configuration, commit, push, delete public files, or import private history.

Public-specific files are protected: `LICENSE`, `README.md`, `AGENTS.md`, `.github/workflows/tests.yml`, the public setup guide and its screenshots, this document, and the privacy/export helpers and their tests. Update these manually in the public checkout so that private deployment defaults, agent rules, CI checks, or license text do not replace the public versions.

Binary files are rejected by default because the text scanner cannot verify their contents. Only after separately inspecting the selected images or other assets, including metadata, use `--allow-binary`. That switch does not certify an image, package, archive, or executable as free of personal information. Installer and release assets need their own audit; a source scan does not check packaged binaries or release downloads.

## 4. Review, stage, and check exactly what will be committed

Review new files as well as changes to existing files. Replace personal addresses, device identifiers, developer team IDs, absolute paths, account names, and deployment data with usable examples. Check screenshots visually and inspect metadata. Confirm that any third-party code can be distributed under the project's license.

```sh
git status --short
git diff --check
git diff
git add -- api/musiclink.py tests/test_music_queue.py
git diff --cached --check
git diff --cached
python3 scripts/check_public_snapshot.py --staged --allow-binary
python3 -m pytest tests/test_music_queue.py tests/test_public_export.py
```

The checker examines the **full staged index**, including unchanged tracked files, and ignores untracked and unstaged files. `--allow-binary` here permits the repository's previously reviewed assets; inspect every added or modified binary separately. Add your local `--denylist` when checking. Do not print or load real provider credentials for tests: use synthetic credentials and isolated test services.

No pattern scanner can prove that a repository contains no personal information. Review the staged diff, prose, filenames, images, and the behavior of changed code before committing. The built-in checks do not detect every email address, person, arbitrary opaque secret, or information hidden in a binary. An empty or partial snapshot also does not establish that the intended changes were checked; confirm the file list and staged status.

## 5. Commit and open a public pull request

Git author and committer fields are public. Before making a commit, use a public identity and an email address you intend to publish. GitHub's account settings provide a private `noreply` address; review whether its associated username is acceptable to publish. These commands configure only this public checkout:

```sh
git config --local user.name 'Public Contributor'
git config --local user.email 'YOUR_GITHUB_NOREPLY_ADDRESS'
git commit -m 'Fix music queue behavior'
git log -1 --format=fuller
git show --stat HEAD
python3 scripts/check_public_snapshot.py --ref HEAD --allow-binary
git push -u origin update/music-fix
gh pr create --base main
```

Replace the example commit message and branch name with descriptions of your change. Verify the commit's author/committer identity and contents before pushing. Review the pull request and its tests, then merge it through the public repository's normal process. For the next update, start a new branch from public `main` and repeat the selected-file export.
