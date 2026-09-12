#!/bin/bash
# Provision a factory-reset RM4 Pro onto a selected Wi-Fi network from a Mac,
# with no Broadlink app and no cloud account involved.
#
# What it does, in order:
#   1. reads the wifi password for the target SSID out of the login keychain
#      (macOS will pop ONE auth dialog -- click Allow);
#   2. joins the Mac's Wi-Fi to the RM4's open setup AP (the network that
#      appeared after the factory reset -- pass its exact name as $1);
#   3. sends the provisioning packet (SSID + password + WPA2) with
#      python-broadlink;
#   4. rejoins the selected Wi-Fi network (remembered network);
#   5. watches for the RM4 to show up with an IP for ~2 minutes.
#
# The rejoin is in an EXIT trap: however this script dies, the Mac's Wi-Fi is
# put back. Run it in Terminal, not from anything that minds ~90s offline:
#
#   bash scripts/provision_rm4.sh "Broadlink_WiFi_Device" "Your Wi-Fi SSID"
# An optional third argument selects the Wi-Fi interface (for example en0).
set -u

BLASTER_AP="${1:?usage: provision_rm4.sh <setup AP name> <target Wi-Fi SSID> [interface]}"
TARGET_SSID="${2:?provide the target Wi-Fi SSID as the second argument}"
WIFI_IF="${3:-$(networksetup -listallhardwareports | awk '/Hardware Port: (Wi-Fi|AirPort)/ {getline; print $2; exit}')}"
if [ -z "$WIFI_IF" ]; then
  echo "!! no Wi-Fi interface found; pass its name as the third argument."
  exit 1
fi
REPO="$(cd "$(dirname "$0")/.." && pwd)"

restore() {
  echo "== rejoining $TARGET_SSID"
  networksetup -setairportnetwork "$WIFI_IF" "$TARGET_SSID" >/dev/null 2>&1
}
trap restore EXIT

echo "== reading the wifi password from the keychain (approve the dialog)"
WIFI_PW="$(security find-generic-password -wa "$TARGET_SSID" 2>/dev/null)"
if [ -z "$WIFI_PW" ]; then
  echo "!! could not read the password for $TARGET_SSID from the keychain."
  echo "   join this network normally first, then rerun and approve the dialog."
  exit 1
fi

echo "== joining the RM4 setup AP: $BLASTER_AP"
networksetup -setairportnetwork "$WIFI_IF" "$BLASTER_AP" || exit 1

# Wait for the AP to hand us a 192.168.10.x address.
for _ in $(seq 1 20); do
  ip="$(ipconfig getifaddr "$WIFI_IF" 2>/dev/null || true)"
  case "$ip" in 192.168.10.*) break ;; esac
  sleep 1
done
echo "   Mac is at ${ip:-<no address -- proceeding anyway>}"

echo "== sending provisioning packet (SSID + WPA2 credentials)"
"$REPO/venv/bin/python" - "$TARGET_SSID" "$WIFI_PW" <<'EOF'
import sys
import broadlink
ssid, password = sys.argv[1], sys.argv[2]
broadlink.setup(ssid, password, 3)  # 3 = WPA2
print("   packet sent -- the RM4 is rebooting onto", ssid)
EOF
status=$?
if [ $status -ne 0 ]; then
  echo "!! provisioning call failed"
  exit $status
fi

restore
trap - EXIT

echo "== waiting for the Mac to be back online, then watching for the RM4"
for _ in $(seq 1 30); do
  ip="$(ipconfig getifaddr "$WIFI_IF" 2>/dev/null || true)"
  case "$ip" in ""|192.168.10.*) ;; *) break ;; esac
  sleep 2
done
echo "   Mac back at ${ip:-<still offline?>}"

"$REPO/venv/bin/python" - <<'EOF'
import time
import broadlink
print("   discovering (up to 120s)...")
deadline = time.time() + 120
while time.time() < deadline:
    devices = broadlink.discover(timeout=8)
    for d in devices:
        mac = ":".join(f"{b:02x}" for b in d.mac)
        print(f"\nFOUND: {type(d).__name__}  host={d.host[0]}  mac={mac}")
        try:
            d.auth()
            print("auth OK -- put this host into config.yaml blaster.host")
        except Exception as exc:
            print("found but auth failed:", exc)
        raise SystemExit(0)
print("\nno RM4 appeared. Its light should be solid when joined. If it is "
      "still blinking, check the Wi-Fi credentials, DHCP service, and "
      "whether the target network allows communication between devices.")
raise SystemExit(1)
EOF
