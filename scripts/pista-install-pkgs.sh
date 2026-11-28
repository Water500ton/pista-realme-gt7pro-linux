#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Multi-distro online package & userspace stack installer for Realme GT7 Pro (pista)
# Supports Debian 13 (trixie), Ubuntu 26.04 (apt-get), and Fedora 45 (dnf).
#
# Installs baseband daemons (rmtfs, tqftpserv, qrtr, libqmi-utils), upstream GPU/Wi-Fi
# firmware (linux-firmware), NetworkManager + wpa_supplicant with clean handoff from
# bootstrap Wi-Fi, Bluetooth, PipeWire audio, libcamera + GStreamer camera ISP stack,
# compiles libpista_ae_guard.so & patched hexagonrpcd, and calibrates camera EEPROMs.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 权限运行: sudo $0"
    exit 1
fi

WAIT_NET=0
[ "${1:-}" = "--wait-online" ] && WAIT_NET=1

check_online() {
    local u code
    for u in "https://mirrors.ustc.edu.cn/" "https://deb.debian.org/" "https://fedoraproject.org/"; do
        if command -v curl >/dev/null 2>&1; then
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$u" 2>/dev/null || echo "000")
            if echo "$code" | grep -qE '^(200|301|302)$'; then
                return 0
            fi
        elif command -v wget >/dev/null 2>&1; then
            if wget -q --spider --timeout=5 "$u" 2>/dev/null; then
                return 0
            fi
        fi
    done
    return 1
}

if [ "$WAIT_NET" -eq 1 ]; then
    [ -f /var/lib/pista-pkgs-done ] && exit 0
    echo "[pista-auto-pkgs] 等待联网后自动安装基带与硬件软件包..."
    while ! check_online; do
        sleep 15
    done
    echo "[pista-auto-pkgs] 检测到网络已连通，开始自动安装..."
fi

echo "=== [1/4] 更新软件源索引 ==="
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update

    echo "=== [2/4] 安装 Debian/Ubuntu 5G 基带与全硬件依赖 ==="
    apt-get install -y \
        network-manager wpasupplicant iw rfkill \
        bluez pipewire pipewire-alsa pipewire-pulse wireplumber alsa-ucm-conf alsa-utils \
        v4l-utils i2c-tools iio-sensor-proxy upower \
        gcc libc6-dev python3-gi python3-numpy python3-pil gir1.2-gtk-3.0 \
        e2fsprogs kmod openssh-server sudo libqmi-utils

    for pkg in firmware-qcom-soc firmware-misc-nonfree linux-firmware \
               rmtfs tqftpserv qrtr-tools \
               libspa-0.2-bluetooth libspa-0.2-libcamera libcamera-tools \
               gstreamer1.0-libcamera gstreamer1.0-gtk3 gstreamer1.0-gl \
               gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools \
               python3-gst-1.0; do
        apt-get install -y "$pkg" 2>/dev/null || echo "[WARN] 可选包 $pkg 暂不可用，跳过"
    done
elif command -v dnf >/dev/null 2>&1; then
    dnf makecache

    echo "=== [2/4] 安装 Fedora 5G 基带与全硬件依赖 ==="
    dnf install -y \
        NetworkManager NetworkManager-tui NetworkManager-wifi wpa_supplicant iw \
        bluez pipewire pipewire-alsa pipewire-pulseaudio wireplumber alsa-ucm alsa-utils \
        v4l-utils i2c-tools iio-sensor-proxy upower \
        gcc glibc-devel python3-gobject python3-numpy python3-pillow gtk3 \
        e2fsprogs kmod openssh-server sudo libqmi-utils linux-firmware

    for pkg in rmtfs tqftpserv qrtr \
               libcamera libcamera-ipa libcamera-tools libcamera-gstreamer \
               pipewire-plugin-libcamera \
               gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-good-gtk \
               python3-gstreamer1; do
        dnf install -y "$pkg" 2>/dev/null || echo "[WARN] 可选包 $pkg 暂不可用，跳过"
    done
else
    echo "[ERROR] 未检测到 apt-get 或 dnf，无法自动安装软件包"
    exit 1
fi

# Remove temporary bootstrap kmod symlinks once native distro kmod is installed
if [ -x /usr/sbin/modprobe ] || [ -x /usr/bin/modprobe ]; then
    for cmd in modprobe insmod lsmod rmmod depmod modinfo; do
        if [ -L "/usr/local/sbin/$cmd" ] && [ "$(readlink "/usr/local/sbin/$cmd" 2>/dev/null || true)" = "/opt/pista-bootstrap/bin/kmod" ]; then
            rm -f "/usr/local/sbin/$cmd"
        fi
    done
fi

# Ensure userdata ext4 is expanded to 100% if resize2fs was unavailable on Day-1 offline boot
if [ ! -f /var/lib/pista-resized ] && command -v resize2fs >/dev/null 2>&1; then
    ROOT_DEV=$(findmnt -n -o SOURCE / 2>/dev/null || true)
    if [ -n "$ROOT_DEV" ]; then
        resize2fs "$ROOT_DEV" >/dev/null 2>&1 && touch /var/lib/pista-resized || true
    fi
fi

echo "=== [3/4] 编译 libpista_ae_guard.so、hexagonrpcd 并执行相机 EEPROM 标定 ==="
if command -v gcc >/dev/null 2>&1 && [ -f /usr/local/share/pista-camera/pista_ae_guard.c ]; then
    gcc -O3 -shared -fPIC /usr/local/share/pista-camera/pista_ae_guard.c \
        -o /usr/local/lib/libpista_ae_guard.so -ldl
    chmod 0755 /usr/local/lib/libpista_ae_guard.so
    echo "[OK] 已编译 /usr/local/lib/libpista_ae_guard.so"
fi

if command -v gcc >/dev/null 2>&1 && [ -f /opt/pista-linux/sensors/hexagonrpc/hexagonrpcd/rpcd.c ]; then
    HEX_SRC=/opt/pista-linux/sensors/hexagonrpc
    gcc -O2 -I"$HEX_SRC/include" \
        "$HEX_SRC"/libhexagonrpc/*.c \
        "$HEX_SRC"/libhexagonrpc/interface/*.c \
        "$HEX_SRC"/hexagonrpcd/*.c \
        "$HEX_SRC"/hexagonrpcd/interface/*.c \
        -o /usr/local/bin/hexagonrpcd
    chmod 0755 /usr/local/bin/hexagonrpcd
    rm -f /etc/systemd/system/hexagonrpcd.service.d/override.conf
    rmdir /etc/systemd/system/hexagonrpcd.service.d 2>/dev/null || true
    cat > /etc/systemd/system/hexagonrpcd.service << 'EOF'
[Unit]
Description=Patched FastRPC daemon for Qualcomm ADSP SSC sensors (Realme GT7 Pro)
After=systemd-modules-load.service systemd-udev-settle.service
ConditionPathExists=/dev/fastrpc-adsp-secure

[Service]
ExecStart=/usr/local/bin/hexagonrpcd -f /dev/fastrpc-adsp-secure -d adsp -R /var/lib/pista-sensors/ -s
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 /etc/systemd/system/hexagonrpcd.service
    echo "[OK] 已编译安装 /usr/local/bin/hexagonrpcd"
fi

if [ -x /usr/local/bin/pista-camera ]; then
    /usr/local/bin/pista-camera --calibrate-eeprom || true
fi

# Re-run firmware stager AFTER linux-firmware install so upstream board-2.bin* conflicts are stripped
[ -x /usr/local/sbin/pista-setup-firmware.sh ] && /usr/local/sbin/pista-setup-firmware.sh || true

echo "=== [4/4] 配置 NetworkManager 接管 Wi-Fi 并启用硬件服务 ==="
# Preserve Day-1 bootstrap Wi-Fi credentials (captured after WAIT_NET in case user connected during first boot)
SAVED_SSID=""
SAVED_PSK=""
if [ -f /etc/wpa_supplicant/pista-wlan0.conf ]; then
    SAVED_SSID=$(sed -n 's/^[[:space:]]*ssid="\([^"]*\)".*/\1/p' /etc/wpa_supplicant/pista-wlan0.conf | head -n 1 || true)
    SAVED_PSK=$(sed -n 's/^[[:space:]]*psk="\([^"]*\)".*/\1/p' /etc/wpa_supplicant/pista-wlan0.conf | head -n 1 || true)
fi

mkdir -p /etc/NetworkManager/conf.d
rm -f /etc/NetworkManager/conf.d/99-pista-iscsi.conf

# Override Ubuntu's default globally-managed-devices restriction so Wi-Fi shows up in desktop UIs
: > /etc/NetworkManager/conf.d/10-globally-managed-devices.conf
chmod 0644 /etc/NetworkManager/conf.d/10-globally-managed-devices.conf

# Keep usb0 (172.16.42.1) and cellular rmnet* out of NetworkManager; explicitly manage Wi-Fi
cat > /etc/NetworkManager/conf.d/99-pista-usb0.conf << 'EOF'
[device-pista-usb0]
match-device=interface-name:usb0;interface-name:rmnet*
managed=0

[device-pista-wifi]
match-device=type:wifi
managed=1
EOF
chmod 0644 /etc/NetworkManager/conf.d/99-pista-usb0.conf

# On Ubuntu, declare NetworkManager as Netplan renderer so Netplan never marks Wi-Fi unmanaged
if [ -d /etc/netplan ]; then
    cat > /etc/netplan/01-pista-nm.yaml << 'EOF'
network:
  version: 2
  renderer: NetworkManager
EOF
    chmod 0600 /etc/netplan/01-pista-nm.yaml
    netplan generate 2>/dev/null || true
fi

# Release wlan0 from bootstrap wpa_supplicant & systemd-networkd before starting/restarting NetworkManager
rm -f /etc/systemd/network/20-wlan0.network
pkill -f "wpa_supplicant.*pista-wlan0.conf" 2>/dev/null || true
sleep 0.4
rm -f /run/wpa_supplicant/wlan0
if [ -L /etc/systemd/system/wpa_supplicant.service ] && [ "$(readlink /etc/systemd/system/wpa_supplicant.service 2>/dev/null || true)" = "/dev/null" ]; then
    rm -f /etc/systemd/system/wpa_supplicant.service
fi
systemctl restart systemd-networkd 2>/dev/null || true

# Disable conflicting / unnecessary daemons
systemctl disable --now ModemManager 2>/dev/null || true
systemctl disable --now open-iscsi iscsi iscsid iscsid.socket pd-mapper 2>/dev/null || true

systemctl daemon-reload
systemctl enable wpa_supplicant NetworkManager 2>/dev/null || true
systemctl restart wpa_supplicant 2>/dev/null || true
systemctl restart NetworkManager 2>/dev/null || true

for svc in qrtr-ns rmtfs tqftpserv bluetooth pista-bt-bdaddr hexagonrpcd iio-sensor-proxy; do
    systemctl enable --now "$svc" 2>/dev/null || true
done

# Seamlessly migrate Day-1 Wi-Fi connection into a managed NetworkManager profile
if [ -n "$SAVED_SSID" ] && command -v nmcli >/dev/null 2>&1; then
    nmcli radio wifi on 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        nm_state=$(nmcli -t -f DEVICE,STATE dev status 2>/dev/null | awk -F: '$1=="wlan0"{print $2; exit}')
        if [ -n "$nm_state" ] && [ "$nm_state" != "unmanaged" ] && [ "$nm_state" != "unavailable" ]; then
            break
        fi
        sleep 1
    done
    nmcli dev wifi rescan 2>/dev/null || true
    for i in 1 2 3 4 5 6 7 8; do
        sleep 1
        if [ "$i" -ge 2 ] && nmcli -t -f SSID dev wifi list 2>/dev/null | grep -Fxq "$SAVED_SSID"; then
            break
        fi
    done
    MIGRATED=0
    if [ -n "$SAVED_PSK" ]; then
        if nmcli dev wifi connect "$SAVED_SSID" password "$SAVED_PSK" 2>/dev/null; then
            MIGRATED=1
        else
            nmcli con delete "$SAVED_SSID" 2>/dev/null || true
            if nmcli con add type wifi ifname wlan0 con-name "$SAVED_SSID" ssid "$SAVED_SSID" \
                wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$SAVED_PSK" 2>/dev/null; then
                nmcli con up "$SAVED_SSID" 2>/dev/null || true
                MIGRATED=1
            fi
        fi
    else
        if nmcli dev wifi connect "$SAVED_SSID" 2>/dev/null; then
            MIGRATED=1
        else
            nmcli con delete "$SAVED_SSID" 2>/dev/null || true
            if nmcli con add type wifi ifname wlan0 con-name "$SAVED_SSID" ssid "$SAVED_SSID" 2>/dev/null; then
                nmcli con up "$SAVED_SSID" 2>/dev/null || true
                MIGRATED=1
            fi
        fi
    fi
    [ "$MIGRATED" -eq 1 ] && rm -f /etc/wpa_supplicant/pista-wlan0.conf
fi

touch /var/lib/pista-pkgs-done
echo "=== 全部基带与硬件软件包自动安装完成！ ==="

# Hand off port 22 cleanly from bootstrap dropbear to native OpenSSH server last
systemctl disable --now pista-bootstrap-ssh 2>/dev/null || true
for svc in sshd ssh; do
    systemctl enable --now "$svc" 2>/dev/null || true
done
