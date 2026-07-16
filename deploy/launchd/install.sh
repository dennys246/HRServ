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
# The repo must be mounted INSIDE the Colima VM: bind-mount sources resolve
# in the VM, and Docker silently fabricates missing sources as empty
# directories — Postgres then crash-loops on "input in flex scanner failed"
# reading a directory where its config should be. Colima only shares $HOME
# and /tmp/colima by default; /opt/hrserv needs an explicit colima.yaml
# mounts entry (see deploy/launchd/README.md §"Key macOS differences").
# Caught live on big-mac-mini 2026-07-15. Only checkable while the VM runs.
if sudo -u "$OPERATOR" -H /opt/homebrew/bin/colima status >/dev/null 2>&1; then
    if ! sudo -u "$OPERATOR" -H /opt/homebrew/bin/colima ssh -- \
            test -f "$HRSERV_DIR/docker/docker-compose.replica.yml" 2>/dev/null; then
        echo "ERROR: $HRSERV_DIR is not visible inside the Colima VM — its bind mounts" >&2
        echo "would become empty directories. Add both \"~\" (QUOTED — bare ~ is YAML" >&2
        echo "null) and /opt/hrserv to the mounts: list in ~/.colima/default/colima.yaml" >&2
        echo "(listing mounts REPLACES the defaults), then colima stop && colima start." >&2
        fail=1
    fi
else
    echo "NOTE: Colima VM not running — could not verify $HRSERV_DIR is mounted inside" >&2
    echo "the VM. Ensure colima.yaml's mounts: include it, or Postgres will crash-loop." >&2
fi

# The hrserv image build needs BuildKit (Dockerfile uses RUN --mount), and
# unlike Linux Docker, Homebrew's docker CLI doesn't bundle buildx. Caught
# live on big-mac-mini's first drill boot 2026-07-15.
if ! sudo -u "$OPERATOR" -H /opt/homebrew/bin/docker buildx version >/dev/null 2>&1; then
    echo "ERROR: 'docker buildx' not available for $OPERATOR — the hrserv image build" >&2
    echo "requires BuildKit. Fix: brew install docker-buildx, and wire" >&2
    echo "cliPluginsExtraDirs if needed (see 'brew info docker-buildx')." >&2
    fail=1
fi
# One check that validates three things at once: the compose plugin is wired
# for the operator, compose is new enough to parse `ports: !override`
# (>= 2.24, required by docker-compose.macos.yml), and docker/.env
# interpolates the role file cleanly. No docker daemon needed for `config`.
if ! sudo -u "$OPERATOR" -H /opt/homebrew/bin/docker compose \
        -f "$HRSERV_DIR/docker/docker-compose.replica.yml" \
        -f "$HRSERV_DIR/docker/docker-compose.macos.yml" \
        config --quiet; then
    echo "ERROR: compose config validation failed for $OPERATOR. Either the brew" >&2
    echo "compose plugin isn't wired (cliPluginsExtraDirs in ~/.docker/config.json;" >&2
    echo "see 'brew info docker-compose'), compose is < 2.24 (can't parse" >&2
    echo "'ports: !override'), or docker/.env is missing required values —" >&2
    echo "the compose error above says which." >&2
    fail=1
fi
[[ $fail -eq 0 ]] || exit 1

if [[ "$LAUNCHD_DIR" != "$HRSERV_DIR/deploy/launchd" ]]; then
    echo "WARNING: running from $LAUNCHD_DIR but the installed daemons will execute" >&2
    echo "scripts from $HRSERV_DIR/deploy/launchd — make sure that checkout is current." >&2
fi

# Resolve the operator's real home dir instead of assuming /Users/<name>.
OPERATOR_HOME="$(dscl . -read "/Users/$OPERATOR" NFSHomeDirectory 2>/dev/null | sed 's/^NFSHomeDirectory: //')"
OPERATOR_HOME="${OPERATOR_HOME:-/Users/$OPERATOR}"

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
    # Render + lint in a temp file FIRST — never leave a malformed plist
    # installed. HOME path is substituted before the bare username so the
    # /Users/<placeholder> pattern still matches.
    rendered="$(mktemp -t "$name")"
    sed -e "s|/Users/REPLACE_WITH_OPERATOR_USER|$OPERATOR_HOME|g" \
        -e "s/REPLACE_WITH_OPERATOR_USER/$OPERATOR/g" \
        "$LAUNCHD_DIR/$name.plist" > "$rendered"
    plutil -lint -s "$rendered"
    install -m 644 -o root -g wheel "$rendered" "$target"
    rm -f "$rendered"
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
