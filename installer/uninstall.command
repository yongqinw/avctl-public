#!/bin/sh
set -eu

APP="/Applications/Avctl Server.app"
USER_NAME="$(/usr/bin/stat -f '%Su' /dev/console)"
KEEP_FLAG=""
if [ "${1:-}" = "--delete-data" ]; then KEEP_FLAG="--delete-data"; fi
"$APP/Contents/Resources/runtime/avctl-core" uninstall-service \
  --user "$USER_NAME" $KEEP_FLAG
echo "The application remains in /Applications and can now be moved to Trash."
