#!/bin/sh
# Install or refresh Print Lab on a Raspberry Pi running Raspberry Pi OS / Debian.
# Run as root from the repo checkout at /opt/printlab/repo. Safe to run again.
set -e
REPO=/opt/printlab/repo
LAB=/var/lib/printlab
PROFILES_SRC=$REPO/print_lab/rootfs/profiles

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

# printer control needs MQTT + AES (Debian packages; the service's venv sees system packages)
dpkg -s python3-paho-mqtt python3-cryptography python3-scipy >/dev/null 2>&1 || {
    apt-get update -qq && apt-get install -y -qq --no-install-recommends python3-paho-mqtt python3-cryptography python3-scipy; }

# service account: no login, no shell
id printlab >/dev/null 2>&1 || useradd --system --home-dir "$LAB" --shell /usr/sbin/nologin printlab
usermod -aG systemd-journal,video printlab
install -d -o printlab -g printlab "$LAB" "$LAB/models" "$LAB/gcode" "$LAB/profiles" "$LAB/work"

# profiles: replace one on disk only if it is an unedited copy of an earlier shipped version
for p in "$PROFILES_SRC"/*.ini; do
    name=$(basename "$p"); dest="$LAB/profiles/$name"
    if [ ! -f "$dest" ]; then
        install -o printlab -g printlab -m 644 "$p" "$dest"
    elif ! cmp -s "$p" "$dest"; then
        sum=$(md5sum "$dest" | cut -d' ' -f1)
        if grep -q "^$sum $name\$" "$PROFILES_SRC/previous.md5"; then
            install -o printlab -g printlab -m 644 "$p" "$dest"; echo "Updated profile $name"
        else
            echo "Profile $name was edited locally; leaving it alone"
        fi
    fi
done

# settings + admin token (created once, never overwritten)
install -d -m 750 -o root -g printlab /etc/printlab
if [ ! -f /etc/printlab/printlab.env ]; then
    token=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)
    cat > /etc/printlab/printlab.env <<ENV
PRINTLAB_DIR=$LAB
PRINTLAB_ADMIN_TOKEN=$token
WATCH_CROPS="0.28,0.2,0.42,0.42;0.28,0.45,0.42,0.42"
WATCH_THREADS=4
ENV
    chown root:printlab /etc/printlab/printlab.env
    chmod 640 /etc/printlab/printlab.env
fi

install -m 755 "$REPO/pi/printlab-update" /usr/local/sbin/printlab-update
install -m 644 "$REPO/pi/printlab.service" "$REPO/pi/printlab-update.service" \
    "$REPO/pi/printlab-update.timer" "$REPO/pi/printlab-bot.service" /etc/systemd/system/
install -m 440 "$REPO/pi/sudoers" /etc/sudoers.d/printlab.new
visudo -cf /etc/sudoers.d/printlab.new && mv /etc/sudoers.d/printlab.new /etc/sudoers.d/printlab

# OrcaSlicer (official ARM64 AppImage, unpacked so it runs without FUSE)
ORCA_VER=2.4.2
if [ "$(cat /opt/orcaslicer/.version 2>/dev/null)" != "$ORCA_VER" ]; then
    tmp=$(mktemp -d)
    if curl -fsSL -o "$tmp/orca.AppImage" \
        "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v$ORCA_VER/OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V$ORCA_VER.AppImage"; then
        chmod +x "$tmp/orca.AppImage"
        (cd "$tmp" && ./orca.AppImage --appimage-extract >/dev/null)
        rm -rf /opt/orcaslicer && mv "$tmp/squashfs-root" /opt/orcaslicer
        echo "$ORCA_VER" > /opt/orcaslicer/.version
        chmod -R a+rX /opt/orcaslicer
    else
        echo "Couldn't download OrcaSlicer $ORCA_VER; keeping what's there"
    fi
    rm -rf "$tmp"
fi

# Claude Code (Anthropic's signed apt repository), for a Remote Control session on this Pi
if ! command -v claude >/dev/null 2>&1; then
    command -v gpg >/dev/null 2>&1 || apt-get install -y -qq gnupg
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o /etc/apt/keyrings/claude-code.asc.new
    if gpg --show-keys --with-colons /etc/apt/keyrings/claude-code.asc.new 2>/dev/null \
            | grep -q '^fpr:::::::::31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE:'; then
        mv /etc/apt/keyrings/claude-code.asc.new /etc/apt/keyrings/claude-code.asc
        echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
            > /etc/apt/sources.list.d/claude-code.list
        apt-get update -qq && apt-get install -y -qq claude-code
    else
        rm -f /etc/apt/keyrings/claude-code.asc.new
        echo "Claude Code signing key didn't match; not installing Claude Code"
    fi
fi
if id robbie >/dev/null 2>&1; then
    install -d -o robbie -g robbie /home/robbie/printlab-agent
    # kept in the repo (pi/agent-CLAUDE.md), so it's refreshed on every update
    install -o robbie -g robbie -m 644 "$REPO/pi/agent-CLAUDE.md" /home/robbie/printlab-agent/CLAUDE.md
    install -m 644 "$REPO/pi/printlab-claude.service" /etc/systemd/system/
fi

systemctl daemon-reload
systemctl enable printlab.service printlab-update.timer printlab-bot.service
systemctl start printlab-update.timer
systemctl restart printlab.service printlab-bot.service
# Claude's Remote Control server: Robbie enables it once after logging in (pi/README.md);
# after that, updates keep Claude Code current and restart it
if systemctl is-enabled --quiet printlab-claude.service 2>/dev/null; then
    apt-get install -y -qq --only-upgrade claude-code || true
    systemctl restart printlab-claude.service || true
fi
echo "Print Lab installed; admin token is in /etc/printlab/printlab.env"
