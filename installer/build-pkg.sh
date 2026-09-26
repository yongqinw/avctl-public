#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${AVCTL_BUILD_PYTHON:-python3}"
OUTPUT="${AVCTL_INSTALLER_OUTPUT:-$ROOT/dist}"
VERSION="${AVCTL_VERSION:-0.0.0}"
APP_SIGN="${AVCTL_APPLICATION_SIGN_IDENTITY:--}"
PKG_SIGN="${AVCTL_INSTALLER_SIGN_IDENTITY:-}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/avctl-installer.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$WORK/pyinstaller-cache}"
export CLANG_MODULE_CACHE_PATH="${CLANG_MODULE_CACHE_PATH:-$WORK/clang-module-cache}"
export SWIFT_MODULECACHE_PATH="${SWIFT_MODULECACHE_PATH:-$CLANG_MODULE_CACHE_PATH}"

if [ "$(uname -m)" != "arm64" ]; then
  echo "Avctl Server release packages currently require an Apple-silicon builder" >&2
  exit 1
fi
# Voice is a first-class Ask capability in the friend installer. A release
# must fail closed instead of silently producing a package whose Hold to talk
# button can never transcribe.
"$PYTHON" -c 'from importlib.metadata import version; from importlib.util import find_spec; assert find_spec("mlx") and find_spec("mlx_whisper") and find_spec("scipy"); assert version("mlx") == "0.23.2"' || {
  echo "install requirements.txt with the build Python; MLX voice runtime is required" >&2
  exit 1
}
MLX_DIR="$("$PYTHON" -c 'from importlib.util import find_spec; print(next(iter(find_spec("mlx").submodule_search_locations)))')"
MLX_WHISPER_DIR="$("$PYTHON" -c 'from pathlib import Path; from importlib.util import find_spec; print(Path(find_spec("mlx_whisper").origin).parent)')"
SCIPY_DIR="$("$PYTHON" -c 'from pathlib import Path; from importlib.util import find_spec; print(Path(find_spec("scipy").origin).parent)')"
mkdir -p "$OUTPUT" "$WORK/pyinstaller" "$WORK/spec" \
  "$WORK/payload/Applications"
# MLX's extension imports private Python helpers dynamically. Copy the
# complete wheel tree as files so those helpers and Metal assets cannot be
# omitted by PyInstaller's static graph, without importing MLX at build time.
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir \
  --name avctl-core --distpath "$WORK/pyinstaller" \
  --workpath "$WORK/build" --specpath "$WORK/spec" \
  --paths "$ROOT" \
  --add-data "$ROOT/api/ui:api/ui" \
  --add-data "$ROOT/configs:configs" \
  --add-data "$MLX_DIR:mlx" \
  --add-data "$MLX_DIR/lib/mlx.metallib:." \
  --add-data "$MLX_WHISPER_DIR/assets:mlx_whisper/assets" \
  --add-data "$SCIPY_DIR/_external:scipy/_external" \
  --collect-submodules api --collect-submodules devices \
  --collect-all roonapi --collect-all opencc \
  --hidden-import mlx_whisper --hidden-import mlx.core \
  --hidden-import mlx.nn --hidden-import mlx.utils \
  --hidden-import mlx._reprlib_fix --hidden-import mlx._os_warning \
  --exclude-module mlx_whisper.torch_whisper --exclude-module torch \
  --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  "$ROOT/installer/entrypoint.py"

APP="$WORK/payload/Applications/Avctl Server.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/runtime" \
  "$APP/Contents/Resources/bin" "$APP/Contents/Resources/apple" "$WORK/scripts"
cp -R "$WORK/pyinstaller/avctl-core/." "$APP/Contents/Resources/runtime/"
cp "$ROOT/LICENSE" "$APP/Contents/Resources/LICENSE"
cp "$ROOT/installer/uninstall.command" "$APP/Contents/Resources/Uninstall avctl.command"
chmod 755 "$APP/Contents/Resources/Uninstall avctl.command"

cat > "$APP/Contents/MacOS/Avctl Server" <<'EOF'
#!/bin/sh
exec "$(dirname "$0")/../Resources/runtime/avctl-core" open-setup
EOF
chmod 755 "$APP/Contents/MacOS/Avctl Server"

xcrun swiftc -O "$ROOT/mac/InputHelper/main.swift" \
  -o "$APP/Contents/Resources/bin/avctl-input-helper"

BRIDGE="$APP/Contents/Resources/apple/AvctlMusicBridge.app"
mkdir -p "$BRIDGE/Contents/MacOS" "$BRIDGE/Contents/Resources"
cp "$ROOT/LICENSE" "$BRIDGE/Contents/Resources/LICENSE"
xcrun swiftc -O -parse-as-library -target arm64-apple-macos14.0 \
  -framework AppKit -framework MusicKit \
  "$ROOT/app/AvctlMusicBridge/main.swift" \
  -o "$BRIDGE/Contents/MacOS/AvctlMusicBridge"
cat > "$BRIDGE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>org.avctl.server.musicbridge</string>
  <key>CFBundleName</key><string>AvctlMusicBridge</string>
  <key>CFBundleDisplayName</key><string>AvctlMusicBridge</string>
  <key>CFBundleExecutable</key><string>AvctlMusicBridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleMusicUsageDescription</key><string>avctl plays Apple Music catalog selections on this Mac without adding them to your library.</string>
</dict></plist>
EOF

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>org.avctl.server</string>
  <key>CFBundleName</key><string>Avctl Server</string>
  <key>CFBundleDisplayName</key><string>Avctl Server</string>
  <key>CFBundleExecutable</key><string>Avctl Server</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF

SIGN_ARGS=(--force --options runtime --sign "$APP_SIGN")
if [ "$APP_SIGN" != "-" ]; then SIGN_ARGS+=(--timestamp); fi
codesign "${SIGN_ARGS[@]}" "$APP/Contents/Resources/bin/avctl-input-helper"
codesign "${SIGN_ARGS[@]}" "$BRIDGE"
codesign --deep "${SIGN_ARGS[@]}" "$APP"
cp "$ROOT/installer/postinstall" "$WORK/scripts/postinstall"
chmod 755 "$WORK/scripts/postinstall"

COMPONENT="$WORK/avctl-component.pkg"
pkgbuild --root "$WORK/payload" --scripts "$WORK/scripts" \
  --identifier org.avctl.server.pkg --version "$VERSION" "$COMPONENT"
PACKAGE_NAME="Avctl-Server-$VERSION.pkg"
PACKAGE="$OUTPUT/$PACKAGE_NAME"
if [ -n "$PKG_SIGN" ]; then
  productsign --sign "$PKG_SIGN" "$COMPONENT" "$PACKAGE"
else
  cp "$COMPONENT" "$PACKAGE"
fi
# Keep the checksum portable after GitHub downloads both assets into a
# different directory. Hashing the absolute build path makes `shasum -c`
# look for a path that only existed on the release runner.
(
  cd "$OUTPUT"
  shasum -a 256 "$PACKAGE_NAME" > "$PACKAGE_NAME.sha256"
)
echo "$PACKAGE"
