#!/bin/bash
set -euo pipefail

PACKAGE="${1:?usage: notarize.sh Avctl-Server-VERSION.pkg}"
: "${AVCTL_NOTARY_APPLE_ID:?set AVCTL_NOTARY_APPLE_ID}"
: "${AVCTL_NOTARY_PASSWORD:?set AVCTL_NOTARY_PASSWORD}"
: "${AVCTL_NOTARY_TEAM_ID:?set AVCTL_NOTARY_TEAM_ID}"

xcrun notarytool submit "$PACKAGE" --wait \
  --apple-id "$AVCTL_NOTARY_APPLE_ID" \
  --password "$AVCTL_NOTARY_PASSWORD" \
  --team-id "$AVCTL_NOTARY_TEAM_ID"
xcrun stapler staple "$PACKAGE"
shasum -a 256 "$PACKAGE" > "$PACKAGE.sha256"
