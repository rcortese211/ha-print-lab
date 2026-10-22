#!/usr/bin/env bash
# Print Lab startup: mount the external drive, mount the toolchain image that
# lives on it (creating it on first run), then hand off to the slice server
# running inside that image.
set -euo pipefail

OPTS=/data/options.json
LABEL=$(jq -r '.drive_label // "HA_Expand"' "$OPTS")
ROOT_GB=$(jq -r '.rootfs_gb // 12' "$OPTS")
SUITE=$(jq -r '.debian_suite // "trixie"' "$OPTS")
# areas of the camera frame the failure check also scores up close (x,y,w,h fractions)
export WATCH_CROPS=$(jq -r '.watch_crops // "0.2,0.15,0.6,0.6;0.2,0.4,0.6,0.6"' "$OPTS")
MIRROR=https://deb.debian.org/debian

DRIVE=/mnt/drive
LAB=$DRIVE/printlab
IMG=$LAB/rootfs.img
ROOT=/opt/root

log() { echo "[print_lab] $*"; }

# ---------------------------------------------------------------- drive
DEV=$(blkid -L "$LABEL" || true)
if [ -z "$DEV" ]; then
    log "No drive labelled '$LABEL' is connected. Waiting for it..."
    for _ in $(seq 1 60); do sleep 10; DEV=$(blkid -L "$LABEL" || true); [ -n "$DEV" ] && break; done
    [ -z "$DEV" ] && { log "Drive still missing; giving up."; exit 1; }
fi
mkdir -p "$DRIVE"
if ! mountpoint -q "$DRIVE"; then
    FSTYPE=$(blkid -p -o value -s TYPE "$DEV" 2>/dev/null || true)
    FSTYPE=${FSTYPE:-exfat}
    log "Mounting $DEV ($FSTYPE) at $DRIVE"
    if ! mount -i -t "$FSTYPE" -o noatime "$DEV" "$DRIVE"; then
        log "Kernel mount failed; trying the FUSE driver"
        if [ "$FSTYPE" = "exfat" ] && mount.exfat-fuse -o noatime "$DEV" "$DRIVE"; then
            log "Mounted with FUSE"
        else
            log "Could not mount $DEV"; exit 1
        fi
    fi
fi
mkdir -p "$LAB"/{models,gcode,profiles,work}

# ---------------------------------------------------------------- toolchain image
if [ ! -f "$IMG" ]; then
    log "Creating ${ROOT_GB} GB toolchain image on the drive (one time)..."
    fallocate -l "${ROOT_GB}G" "$IMG" 2>/dev/null || truncate -s "${ROOT_GB}G" "$IMG"
    mkfs.ext4 -q -F -L printlab-root "$IMG"
fi
mkdir -p "$ROOT"
mountpoint -q "$ROOT" || mount -o loop,noatime "$IMG" "$ROOT"

if [ ! -f "$ROOT/.printlab-ready" ]; then
    log "Installing Debian $SUITE + PrusaSlicer + OpenSCAD into the image. This takes a while on first start."
    if [ ! -x "$ROOT/usr/bin/apt-get" ]; then
        debootstrap --variant=minbase --arch=arm64 --no-check-gpg \
            --include=ca-certificates "$SUITE" "$ROOT" "$MIRROR"
    fi
fi

# bind the kernel filesystems and the data folders into the image
for d in proc sys dev; do
    mkdir -p "$ROOT/$d"
    mountpoint -q "$ROOT/$d" || mount --rbind "/$d" "$ROOT/$d"
done
mkdir -p "$ROOT/lab"
mountpoint -q "$ROOT/lab" || mount --bind "$LAB" "$ROOT/lab"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"

if [ ! -f "$ROOT/.printlab-ready" ]; then
    chroot "$ROOT" /bin/sh -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends prusa-slicer openscad python3 xvfb xauth
        apt-get clean
    "
    touch "$ROOT/.printlab-ready"
    log "Toolchain installed."
fi

# failure detection: ffmpeg for frames, onnxruntime for the model (one time)
if [ ! -f "$ROOT/.printlab-ai-ready" ]; then
    log "Installing the print-failure detector (ffmpeg + onnxruntime) into the image..."
    chroot "$ROOT" /bin/sh -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y --no-install-recommends ffmpeg python3-numpy python3-venv python3-pip
        apt-get clean
        python3 -m venv --system-site-packages /opt/ai
        /opt/ai/bin/pip install --no-cache-dir onnxruntime
    "
    touch "$ROOT/.printlab-ai-ready"
    log "Detector installed."
fi

# ship code and default profiles. A profile on the drive is only replaced when it
# is an unedited copy of an earlier shipped version (listed in previous.md5).
mkdir -p "$ROOT/srv"
cp /opt/printlab-files/server.py /opt/printlab-files/watch.py "$ROOT/srv/"
PREV=/opt/printlab-files/profiles/previous.md5
for p in /opt/printlab-files/profiles/*.ini; do
    name=$(basename "$p"); dest="$LAB/profiles/$name"
    if [ ! -f "$dest" ]; then
        cp "$p" "$dest"
    elif ! cmp -s "$p" "$dest"; then
        sum=$(md5sum "$dest" | cut -d' ' -f1)
        if [ -f "$PREV" ] && grep -q "^$sum $name\$" "$PREV"; then
            cp "$p" "$dest"; log "Updated profile $name"
        else
            log "Profile $name was edited on the drive; leaving it alone"
        fi
    fi
done

log "Starting slice server on :8099"
exec chroot "$ROOT" /opt/ai/bin/python3 -u /srv/server.py
