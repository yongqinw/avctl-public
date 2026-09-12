# Working on avctl

- Keep credentials, invitations, personal device addresses, signing identities,
  local paths, and runtime state out of public changes. Use neutral examples in
  documentation and synthetic credentials in tests.
- Do not inspect provider credential values from the environment or read the
  user's credential files while verifying changes. Run tests with isolated
  configuration, state directories, and ports; do not use the installed Core.
- Import private changes as selected file snapshots on a branch based on this
  public repository. Do not merge private Git history or copy a private `.git`
  directory. Follow `docs/public-contributions.md`.
- Run the public snapshot privacy checker on staged changes before publishing,
  and inspect screenshots and other binary assets separately. Automated pattern
  checks complement review; they cannot prove that every secret is absent.
- Preserve third-party copyright and license notices. Project contributions
  use GPL-3.0-only; see `LICENSE`.
