#!/bin/sh
# Install or refresh Print Lab on a Raspberry Pi running Raspberry Pi OS / Debian.
# Run as root from the repo checkout at /opt/printlab/repo. Safe to run again.
set -e
REPO=/opt/printlab/repo
LAB=/var/lib/printlab
PROFILES_SRC=$REPO/print_lab/rootfs/profiles

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

# printer control needs MQTT + AES (Debian packages; the service's venv sees system packages)
dpkg -s python3-paho-mqtt python3-cryptography >/dev/null 2>&1 || {
    apt-get update -qq && apt-get install -y -qq --no-install-recommends python3-paho-mqtt python3-cryptography; }

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

systemctl daemon-reload
systemctl enable printlab.service printlab-update.timer printlab-bot.service
systemctl start printlab-update.timer
systemctl restart printlab.service printlab-bot.service
echo "Print Lab installed; admin token is in /etc/printlab/printlab.env"
