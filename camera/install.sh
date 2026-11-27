#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing libcamera, PipeWire SPA plugin, GStreamer GTK3, and Realme GT7 Pro ISP stack..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    v4l-utils i2c-tools libcamera-tools libspa-0.2-libcamera \
    gstreamer1.0-libcamera gstreamer1.0-gtk3 gstreamer1.0-gl \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools \
    python3-gi python3-gst-1.0 python3-pil python3-numpy gir1.2-gtk-3.0

install -d /usr/local/share/pista-camera/ipa/simple /usr/share/libcamera/ipa/simple /usr/local/lib
install -m 0755 "$SCRIPT_DIR/pista-camera" /usr/local/bin/pista-camera
install -m 0644 "$SCRIPT_DIR/pista_photo_isp.py" /usr/local/share/pista-camera/pista_photo_isp.py
install -m 0644 "$SCRIPT_DIR/pista_isp_profiles.json" /usr/local/share/pista-camera/pista_isp_profiles.json
install -m 0644 "$SCRIPT_DIR/ipa/"*.yaml /usr/share/libcamera/ipa/simple/
install -m 0644 "$SCRIPT_DIR/ipa_pista/simple/"*.yaml /usr/local/share/pista-camera/ipa/simple/
if command -v gcc >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/pista_ae_guard.c" ]; then
    gcc -O2 -shared -fPIC "$SCRIPT_DIR/pista_ae_guard.c" -o /usr/local/lib/libpista_ae_guard.so -ldl
fi
install -m 0644 "$SCRIPT_DIR/pista-camera.desktop" /usr/share/applications/pista-camera.desktop
install -m 0644 "$SCRIPT_DIR/99-pista-camera.rules" /etc/udev/rules.d/99-pista-camera.rules

udevadm control --reload-rules
udevadm trigger --subsystem-match=dma_heap --subsystem-match=misc 2>/dev/null || true

if [ -n "${SUDO_USER:-}" ]; then
    usermod -aG video,render "$SUDO_USER" 2>/dev/null || true
fi

# Read per-unit factory AF & 17x13 LSC calibration from GT24P128G EEPROMs (non-fatal if VCMs not loaded yet)
/usr/local/bin/pista-camera --calibrate-eeprom || true

echo "Done! Launch 'Camera' (pista-camera) from the desktop or run /usr/local/bin/pista-camera."
