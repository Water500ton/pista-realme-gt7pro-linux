#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing libcamera, PipeWire SPA plugin, and GStreamer GTK3 camera stack..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    v4l-utils libcamera-tools libspa-0.2-libcamera \
    gstreamer1.0-libcamera gstreamer1.0-gtk3 gstreamer1.0-gl \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools \
    python3-gi python3-gst-1.0 python3-pil gir1.2-gtk-3.0

install -m 0755 "$SCRIPT_DIR/pista-camera" /usr/local/bin/pista-camera
install -m 0644 "$SCRIPT_DIR/pista-camera.desktop" /usr/share/applications/pista-camera.desktop
install -m 0644 "$SCRIPT_DIR/99-pista-camera.rules" /etc/udev/rules.d/99-pista-camera.rules

udevadm control --reload-rules
udevadm trigger --subsystem-match=dma_heap --subsystem-match=misc 2>/dev/null || true

if [ -n "${SUDO_USER:-}" ]; then
    usermod -aG video,render "$SUDO_USER" 2>/dev/null || true
fi

echo "Done! Launch 'Camera' (pista-camera) from the desktop or run /usr/local/bin/pista-camera."
