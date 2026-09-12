# avctl Input Helper

This Swift executable posts validated keyboard and pointer events through
CoreGraphics in the logged-in user's WindowServer session. It accepts events
only on the mode-0600 `~/.avctl/mac-input.sock` Unix socket.

The packaged Core and input helper use per-user LaunchAgents. Selecting the
optional Mac mini panel installs and starts the helper's LaunchAgent; disabling
that panel removes it. The packaged helper lives at
`/Applications/Avctl Server.app/Contents/Resources/bin/avctl-input-helper`.

The helper needs Accessibility permission. It requests access on first start;
macOS records the decision for the signed helper executable. It does not work
before login or on the lock screen.

For a development build from the repository root:

```sh
xcrun swiftc -O -o avctl-input-helper mac/InputHelper/main.swift
codesign --force --sign - --identifier com.avctl.input-helper avctl-input-helper
```

The package manages service registration through `installer/entrypoint.py`.
For package construction and optional helper setup, see the
[Core installer guide](../../docs/core-installer.md).
