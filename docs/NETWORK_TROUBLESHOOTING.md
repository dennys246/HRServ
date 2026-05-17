# NETWORK_TROUBLESHOOTING.md

Playbook for diagnosing network issues on `jib-jab` (the primary HRServ host) after
relocation, reboot, or seemingly random connectivity failures. Distilled from the
multi-hour outage on 2026-05-16 — read this BEFORE chasing the same rabbit holes again.

## Architecture in one paragraph

`jib-jab` is a mobile Debian 13 box on residential WiFi behind an Arris mesh router.
Network stack: `NetworkManager` manages the WiFi interface (`wlp5s0`, Intel AX200);
`wpa_supplicant` runs in D-Bus mode (NetworkManager-controlled). `tailscaled` provides
a private mesh network. `cloudflared` provides outbound-only HTTPS tunneling for
HRServ (no inbound ports exposed). `fail2ban` protects sshd. Remote SSH is via Tailscale
(`ssh dennys@jib-jab.org` resolves to the **tailnet IP**, not public IP — public IP
changes never break SSH).

## Sentinel commands — confirm health in 30 seconds

On jib-jab (physical or already-SSH'd):

```bash
# WiFi managed cleanly?
nmcli device status                                       # wlp5s0 should be 'connected'
ps aux | grep wpa_supplicant | grep -v grep              # exactly ONE process, '-u -s -O /run/wpa_supplicant'
systemctl is-active NetworkManager wpa_supplicant tailscaled docker

# Network reachability?
tailscale status                                         # peers visible, jib-jab self-IP shown
ip -4 addr show wlp5s0                                   # WiFi LAN IP (192.168.x.x) on the right interface
ip -4 addr show tailscale0                               # tailnet IP (100.x.x.x) bound to tailscale0
ip route get 1.1.1.1                                     # default route exists

# Boot-time wait-for-tailscale drop-in fired correctly?
journalctl -u docker -b 0 | grep -i 'tailscale wait'     # should show the ExecStartPre running
systemctl cat docker | grep -A 3 wait-for-tailscale      # confirm drop-in is merged into the unit

# fail2ban not biting us?
sudo fail2ban-client status sshd                         # banned IP list shouldn't include trusted hosts
cat /etc/fail2ban/jail.d/ignore-trusted.conf             # ignoreip line present

# HRServ stack?
docker compose -f /opt/hrserv/docker/docker-compose.primary.yml ps   # all three up (healthy)
curl -fsS https://api.hrfunc.org/healthz                 # 200 OK from anywhere
```

If all those check out, the box is fine.

## SSH from Mac to jib-jab is hanging

The 2026-05-16 outage cost hours because we chased the wrong layers. Diagnose **in this order**:

### 1. fail2ban (fastest cause to rule out)

```bash
# On jib-jab
sudo fail2ban-client status sshd
```

Look at "Banned IP list". If your Mac's LAN IP or Tailscale IP (100.103.9.109 etc.) is listed,
unban:

```bash
sudo fail2ban-client set sshd unbanip <IP>
```

Then verify the ignore-trusted config exists:

```bash
cat /etc/fail2ban/jail.d/ignore-trusted.conf
```

If it doesn't, create it (this should already be there per CLAUDE.md):

```bash
sudo tee /etc/fail2ban/jail.d/ignore-trusted.conf > /dev/null <<'EOF'
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 192.168.0.0/16 10.0.0.0/8 100.64.0.0/10
EOF
sudo systemctl restart fail2ban
```

**Why this is #1**: fail2ban drops banned-IP packets in iptables before journalctl sees them.
SSH attempts vanish silently. No log lines. Looks like every other "packets aren't reaching
sshd" symptom, but it's the most common cause and the fastest to verify.

### 2. Router stale device entries (Arris admin)

Open `http://192.168.0.1` (Arris admin). Devices → look for jib-jab. If there are
**multiple entries** with different MACs, that's the problem:
- jib-jab's WiFi MAC has been changing (randomization)
- Router routes inbound packets to ghost MACs that don't respond
- Outbound from jib-jab works because the router learns the current MAC from source frames
- ICMP can appear to work (especially `tailscale ping` which uses Tailscale's daemon protocol)
- Real TCP from Mac to jib-jab silently fails

**Fix**:
1. Delete all but the current entry (the one matching jib-jab's actual hardware MAC).
2. Add a DHCP reservation: bind jib-jab's hardware MAC to a fixed IP (e.g., 192.168.0.50).
3. On jib-jab, pin the MAC permanent so it never randomizes again:

```bash
nmcli connection show   # get the connection name
sudo nmcli connection modify "<conn-name>" 802-11-wireless.cloned-mac-address permanent
sudo nmcli connection modify "<conn-name>" 802-11-wireless.powersave 2
sudo nmcli connection down "<conn-name>"
sudo nmcli connection up "<conn-name>"
```

### 3. NetworkManager / wpa_supplicant boot state

After a reboot, if `nmcli device status` shows `wlp5s0  wifi  unavailable`:

```bash
# Are there multiple wpa_supplicant processes? Should be ONE in D-Bus mode.
ps aux | grep wpa_supplicant | grep -v grep
```

A standalone instance (`wpa_supplicant -B -i wlp5s0 ...`) means the ifupdown hook is
re-launching it. The script is `/etc/wpa_supplicant/ifupdown.sh` (symlinked from
`/etc/network/if-pre-up.d/wpasupplicant`). Make it non-executable:

```bash
sudo chmod -x /etc/wpa_supplicant/ifupdown.sh
sudo pkill -f 'wpa_supplicant.*-i wlp5s0'
sudo systemctl restart NetworkManager
```

Also confirm:
- `networking.service` masked (not just disabled)
- `wpa_supplicant.service` **enabled** (D-Bus activation needs it — counter-intuitive)

```bash
systemctl is-enabled NetworkManager wpa_supplicant networking
# Expected output: enabled, enabled, masked
```

If `networking` shows `enabled` or `static`:

```bash
sudo systemctl disable --now networking
sudo systemctl mask networking
```

If `wpa_supplicant.service` shows `masked`:

```bash
sudo systemctl unmask wpa_supplicant
sudo systemctl enable --now wpa_supplicant
sudo systemctl restart NetworkManager
```

### 4. iwlwifi instability (occasional but visible)

If `dmesg` or `journalctl` shows lines like:
> `iwlwifi 0000:05:00.0: Not associated and the session protection is over already`

The WiFi card is flapping associations. Most common cause is aggressive power management:

```bash
iw dev wlp5s0 get power_save             # if 'on', that's the problem
sudo nmcli connection modify "<conn-name>" 802-11-wireless.powersave 2
sudo nmcli connection down "<conn-name>" && sudo nmcli connection up "<conn-name>"
```

Also ensure firmware is current:

```bash
sudo apt install --reinstall firmware-iwlwifi
sudo modprobe -r iwlwifi && sudo modprobe iwlwifi
```

### 5. Tailscale daemon down

```bash
sudo systemctl status tailscaled
tailscale status   # if 'Tailscale is stopped' or empty, daemon is broken
```

If down:
```bash
sudo systemctl restart tailscaled
sleep 2
sudo tailscale up        # follow auth URL if prompted
```

**Important**: `tailscale ping <peer>` succeeding does NOT prove TCP-over-Tailscale works.
That command uses the daemon's protocol. Use `nc -vz <peer-tailnet-ip> 22` for a real TCP test.

## Common false trails (don't waste time here)

These look promising but aren't usually the cause:

- **AP Isolation on the Arris** — it's off by default on the main SSID and Arris admin should
  show it unchecked on all bands. If ICMP works, it's not AP Isolation.
- **iptables filter rules** — both INPUT chain (with `policy ACCEPT` test) and `ts-input`
  chain are easy to audit and have never been the actual cause on this box.
- **sshd config** — sshd accepts on `0.0.0.0:22`. If `ssh dennys@localhost` works on jib-jab
  but external SSH fails, sshd is fine; problem is upstream of sshd.
- **MTU / MSS clamping** — would affect data transfer, not initial TCP handshake.

## Hard reset of jib-jab networking (nuclear option)

If everything above checks out and SSH still fails, the box has accumulated bad state
somewhere we haven't found. Last resort:

```bash
# Flush all firewall state
sudo iptables -F && sudo iptables -X
sudo iptables -t nat -F && sudo iptables -t nat -X
sudo iptables -t mangle -F && sudo iptables -t mangle -X
sudo iptables -P INPUT ACCEPT
sudo iptables -P FORWARD ACCEPT
sudo iptables -P OUTPUT ACCEPT
sudo nft flush ruleset

# Restart everything network-related — docker and tailscale rebuild their iptables rules
sudo systemctl restart NetworkManager docker tailscaled fail2ban

# Reboot if still broken (clears conntrack table + everything else)
sudo reboot
```

This is also a useful test for PR #8's reboot-clean orchestration — `hrserv.service` should
bring the stack up automatically after reboot with no manual `dc up`.

## Recovery procedure after physical relocation

When jib-jab is moved to a new location with a new WiFi network:

1. **Physical access** — keyboard + monitor on jib-jab, since WiFi may not auto-connect.
2. **Add the new WiFi**:
   ```bash
   nmcli device wifi list
   sudo nmcli device wifi connect "<NEW_SSID>" password "<PWD>"
   sudo nmcli connection modify "<NEW_SSID>" 802-11-wireless.cloned-mac-address permanent
   sudo nmcli connection modify "<NEW_SSID>" 802-11-wireless.powersave 2
   sudo nmcli connection down "<NEW_SSID>" && sudo nmcli connection up "<NEW_SSID>"
   ```
3. **Confirm Tailscale reconnected**:
   ```bash
   tailscale status
   # If stopped: sudo systemctl restart tailscaled && sudo tailscale up
   ```
4. **Hostname/MAC** — note `hostname -I` and `ip link show wlp5s0 | grep ether` for the
   new router setup.
5. **On the new router** (admin UI):
   - Add a DHCP reservation: jib-jab's hardware MAC → fixed LAN IP.
   - No port forwarding needed (Tailscale handles remote access).
6. **From your Mac** (anywhere with Tailscale connected):
   ```bash
   ssh dennys@jib-jab.org   # routes via Tailscale, no DNS update needed
   ```
   The `jib-jab.org` A record points to the tailnet IP, not the public IP, so it
   keeps working regardless of which network jib-jab is on.

## Why we have so many notes here

The 2026-05-16 outage took several hours of diagnosis to resolve. Each individual cause
was plausibly the culprit, and fixing one revealed the next. If you're reading this during
a similar outage, follow the order above strictly — fail2ban first, router stale entries
second, NM/wpa_supplicant boot state third. That's the order that would have taken 20
minutes instead of several hours.
