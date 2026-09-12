#!/bin/bash
# One local command for the installer/device/music/Ask acceptance matrix.
#
# The default path is entirely synthetic: conftest replaces every hardware
# driver and every provider credential, so running this cannot wake a TV,
# change a queue, or produce sound.  --full expands the matrix to the entire
# repository suite.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${AVCTL_TEST_PYTHON:-"$ROOT/venv/bin/python"}
MODE=${1:---focused}

if [ ! -x "$PYTHON" ]; then
  echo "Set AVCTL_TEST_PYTHON to the project's virtualenv Python." >&2
  exit 2
fi

cd "$ROOT"

case "$MODE" in
  --focused)
    "$PYTHON" -m pytest -q \
      tests/test_installer.py \
      tests/test_installer_live_acceptance.py \
      tests/test_setup.py \
      tests/test_registry.py \
      tests/test_volume_caps.py \
      tests/test_music_queue.py \
      tests/test_music_explore.py \
      tests/test_apple_music_scripts.py \
      tests/test_apple_ask_combo.py \
      tests/test_roon_drivers.py \
      tests/test_roon_ask_combo.py \
      tests/test_agent_providers.py \
      tests/test_apple_broker.py
    ;;
  --full)
    "$PYTHON" -m pytest -q
    ;;
  *)
    echo "usage: $0 [--focused|--full]" >&2
    exit 2
    ;;
esac
