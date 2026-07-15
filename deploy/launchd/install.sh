#!/bin/bash
# install.sh — install the HRServ macOS boot chain (launchd flavor).
#
# macOS equivalent of the systemd install steps in deploy/hrserv.service and
# deploy/docker.service.d/wait-for-tailscale.conf. Renders the operator
# username into the plists and copies them to /Library/LaunchDaemons.
#
# Deliberately does NOT bootstrap (start) the daemons: com.hrfunc.hrserv
# runs `compose down && up -d` the moment it loads, which would kill a
# manually-started stack — the same trap deploy/hrserv.service warns about
# ("don't systemctl start from the terminal you ran dc up -d in"). The next
# reboot activates everything; a deliberate reboot IS the verification
# drill. See deploy/launchd/README.md.
#
# Usage, from the operator account (not a root shell):
#   sudo /opt/hrserv/deploy/launchd/install.sh
set -euo pipefail

HRSERV_DIR="/opt/hrserv"
LAUNCHD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMONS=(com.hrfunc.colima com.hrfunc.hrserv)

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run with sudo (installing to /Library/LaunchDaemons needs root)." >&2
    exit 1
fi
OPERATOR="${SUDO_USER:-}"
if [[ -z "$OPERATOR" || "$OPERATOR" == "root" ]]; then
    echo "ERROR: run via sudo from the operator account, not from a root shell —" >&2
    echo "the daemons run as that user (Colima state lives in their home dir)." >&2
    exit 1
fi

# --- Preconditions (mirrors the PRECONDITION block in wait-for-tailscale.conf)
fail=0
for bin in /opt/homebrew/bin/tailscale /opt/homebrew/bin/colima /opt/homebrew/bin/docker; do
    if [[ ! -x "$bin" ]]; then
        echo "ERROR: missing $bin — brew install $(basename "$bin")" >&2
        fail=1
    fi
done
if [[ ! -f "$HRSERV_DIR/docker/.env" ]]; then
    echo "ERROR: $HRSERV_DIR/docker/.env missing — do NEW_NODE_SETUP Step 7 first." >&2
    fail=1
fi
if ! sudo -u "$OPERATOR" -H /opt/homebrew/bin/docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' not working for $OPERATOR — wire the brew compose" >&2
    echo "plugin (cliPluginsExtraDirs in ~/.docker/config.json; see 'brew info docker-compose')." >&2
    fail=1
fi
[[ $fail -eq 0 ]] || exit 1

# FileVault halts boot at the disk-unlock screen — fatal for headless reboots.
if fdesetup status | grep -q "FileVault is On"; then
    echo "WARNING: FileVault is ON. This box will stop at the unlock screen on every" >&2
    echo "reboot and the boot chain will never run. Disable it: sudo fdesetup disable" >&2
fi

# --- Install
mkdir -p "$HRSERV_DIR/logs"
chown "$OPERATOR" "$HRSERV_DIR/logs"
chmod 755 "$LAUNCHD_DIR"/bin/*.sh

for name in "${DAEMONS[@]}"; do
    target="/Library/LaunchDaemons/$name.plist"
    sed "s/REPLACE_WITH_OPERATOR_USER/$OPERATOR/g" "$LAUNCHD_DIR/$name.plist" > "$target"
    chown root:wheel "$target"
    chmod 644 "$target"
    plutil -lint -s "$target"
    # Never bootout here: on a live host that would kill the running Colima
    # VM (and with it the stack). launchd re-reads /Library/LaunchDaemons at
    # boot, so the updated plist simply takes effect on the next reboot.
    if launchctl print "system/$name" >/dev/null 2>&1; then
        echo "installed $target (runs as $OPERATOR) — currently-loaded version keeps running; new plist takes effect on next reboot"
    else
        echo "installed $target (runs as $OPERATOR)"
    fi
done

cat <<EOF

Installed. The daemons activate on the next boot — reboot deliberately and
verify (see deploy/launchd/README.md §Verify):

    sudo reboot
    # after ~2-3 minutes, from another machine:
    #   ssh in, then:
    launchctl print system/com.hrfunc.colima | grep -E 'state|last exit'
    tail -20 $HRSERV_DIR/logs/launchd-hrserv.log

One-time host hygiene if not done yet (README §Host settings):
    sudo pmset -a sleep 0 displaysleep 10 autorestart 1
EOF
