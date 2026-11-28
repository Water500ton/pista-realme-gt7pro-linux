#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# One-click OFFLINE read-only firmware & hardware-config provisioning script for
# Realme GT7 Pro (pista / SM8750) running Mainline Linux 7.3.6 (Ubuntu / Debian / Fedora).
#
# Safety:
# - Mounts stock UFS partitions strictly READ-ONLY (-o ro / losetup -r).
# - Never modifies Android partitions, EFS/NV, or A/B slots.
# - 100% offline: embeds a pure-Python ELF PIL-squasher and Android super LP
#   metadata reader so all firmware is extracted cleanly on boot with no network.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 权限运行: sudo $0"
    exit 1
fi

SLOT="a"
if grep -q "androidboot.slot_suffix=_b" /proc/cmdline 2>/dev/null; then
    SLOT="b"
fi
ALT_SLOT="b"
[ "$SLOT" = "b" ] && ALT_SLOT="a"

MNT_BASE="/tmp/pista_fw_mnt"
FW_PISTA="/lib/firmware/qcom/sm8750/realme/pista"
FW_WIFI="/lib/firmware/ath12k/WCN7860/hw2.0"
FW_BT="/lib/firmware/qca"
FW_QCOM="/lib/firmware/qcom"
RMTFS_DIR="/var/lib/pista-rmtfs"
SENS_DIR="/var/lib/pista-sensors"
CDSP_DIR="/usr/lib/qcom/cdsp"

mkdir -p "$MNT_BASE" "$FW_PISTA" "$FW_WIFI" "$FW_BT" "$FW_QCOM" \
         "$RMTFS_DIR" "$CDSP_DIR" \
         "$SENS_DIR/sensors/registry" "$SENS_DIR/sensors/config" \
         "$SENS_DIR/dsp/adsp" "$SENS_DIR/socinfo"

LOOP_DEVS=()
LAST_MOUNT_DIR=""
cleanup() {
    for d in "$MNT_BASE"/*; do
        mountpoint -q "$d" 2>/dev/null && umount "$d" 2>/dev/null || true
    done
    if [ "${#LOOP_DEVS[@]}" -gt 0 ]; then
        for lo in "${LOOP_DEVS[@]}"; do
            [ -n "$lo" ] && losetup -d "$lo" 2>/dev/null || true
        done
    fi
    rm -rf "$MNT_BASE"
}
trap cleanup EXIT

find_part_dev() {
    local partname="$1"
    for ue in /sys/block/sd*/sd*/uevent /sys/class/block/*/uevent; do
        [ -r "$ue" ] || continue
        if grep -q "^PARTNAME=${partname}$" "$ue" 2>/dev/null; then
            local bname
            bname=$(awk -F= '/^DEVNAME=/{print $2}' "$ue")
            [ -z "$bname" ] && bname=$(basename "$(dirname "$ue")")
            if [ -b "/dev/$bname" ]; then
                echo "/dev/$bname"
                return 0
            fi
        fi
    done
    return 1
}

mount_ro() {
    local partname="$1"
    local fstype="${2:-auto}"
    local mnt="$MNT_BASE/$partname"
    local dev
    dev=$(find_part_dev "$partname" || true)
    if [ -z "$dev" ]; then
        return 1
    fi
    mkdir -p "$mnt"
    if [ "$fstype" = "auto" ]; then
        mount -o ro "$dev" "$mnt" 2>/dev/null || return 1
    else
        mount -t "$fstype" -o ro "$dev" "$mnt" 2>/dev/null || return 1
    fi
    LAST_MOUNT_DIR="$mnt"
    echo "[OK] 只读挂载 $partname ($dev)"
    return 0
}

mount_slot_ro() {
    local base="$1"
    local fstype="${2:-auto}"
    local s
    for s in "$SLOT" "$ALT_SLOT"; do
        if [ "$fstype" != "auto" ] && mount_ro "${base}_${s}" "$fstype"; then
            return 0
        fi
        if mount_ro "${base}_${s}" "auto"; then
            return 0
        fi
    done
    echo "[WARN] 未找到或无法挂载分区 ${base}_${SLOT}/${ALT_SLOT}，跳过"
    return 1
}

# Pure-Python PIL ELF squasher (merges <name>.mdt + <name>.bXX -> <name>.mbn)
pil_squash() {
    local mdt="$1"
    local out="$2"
    [ -f "$mdt" ] || return 1
    [ -s "$out" ] && return 0
    python3 - "$mdt" "$out" << 'PYEOF'
import os, struct, sys

mdt_path, out_path = sys.argv[1], sys.argv[2]
base = mdt_path[:-4] if mdt_path.endswith(".mdt") else mdt_path
mdt = open(mdt_path, "rb").read()
if mdt[:4] != b"\x7fELF":
    sys.exit(1)

ei_class = mdt[4]
if ei_class == 1:  # ELF32
    e_phoff, = struct.unpack_from("<I", mdt, 28)
    e_phentsize, e_phnum = struct.unpack_from("<HH", mdt, 42)
    ph_fmt = "<IIIIIIII"
    idx_off, idx_fsz, idx_flg = 1, 4, 6
elif ei_class == 2:  # ELF64
    e_phoff, = struct.unpack_from("<Q", mdt, 32)
    e_phentsize, e_phnum = struct.unpack_from("<HH", mdt, 54)
    ph_fmt = "<IIQQQQQQ"
    idx_off, idx_fsz, idx_flg = 2, 5, 1
else:
    sys.exit(1)

max_end = len(mdt)
segs = []
for i in range(e_phnum):
    ph = struct.unpack_from(ph_fmt, mdt, e_phoff + i * e_phentsize)
    off, fsz, flg = ph[idx_off], ph[idx_fsz], ph[idx_flg]
    segs.append((i, off, fsz, flg))
    if fsz > 0 and off + fsz > max_end:
        max_end = off + fsz

out = bytearray(max_end)
out[:len(mdt)] = mdt
for i, off, fsz, flg in segs:
    if fsz == 0:
        continue
    bpath = f"{base}.b{i:02d}"
    if os.path.isfile(bpath):
        data = open(bpath, "rb").read()
        out[off:off + len(data)] = data
    elif (flg & (0x7 << 24)) == (0x2 << 24):
        # Hash segment is already embedded inside .mdt
        pass

tmp_out = out_path + ".tmp"
with open(tmp_out, "wb") as f:
    f.write(out)
os.replace(tmp_out, out_path)
PYEOF
    echo "  [PIL] $(basename "$mdt") -> $out ($(du -h "$out" | cut -f1))"
}

# Pure-Python Android super LP metadata parser: prints "<offset_bytes> <size_bytes>" for a logical partition
find_super_extent() {
    local super_dev="$1"
    local target_name="$2"
    python3 - "$super_dev" "$target_name" << 'PYEOF'
import struct, sys

dev, target = sys.argv[1], sys.argv[2]
with open(dev, "rb") as f:
    f.seek(4096)
    geom = f.read(4096)
    magic, = struct.unpack_from("<I", geom, 0)
    if magic != 0x616c4467:  # LP_METADATA_GEOMETRY_MAGIC
        f.seek(8192)
        geom = f.read(4096)
        magic, = struct.unpack_from("<I", geom, 0)
        if magic != 0x616c4467:
            sys.exit(1)
    max_meta_size, meta_slot_count, _ = struct.unpack_from("<III", geom, 40)
    for slot_idx in range(max(1, meta_slot_count)):
        base = 12288 + slot_idx * max_meta_size
        f.seek(base)
        hdr = f.read(256)
        if len(hdr) < 104:
            continue
        hm, major, minor, hdr_size = struct.unpack_from("<IHHI", hdr, 0)
        if hm != 0x414C5030:  # LP_METADATA_HEADER_MAGIC
            continue
        tables_size, = struct.unpack_from("<I", hdr, 44)
        p_off, p_num, p_ent = struct.unpack_from("<III", hdr, 80)
        e_off, e_num, e_ent = struct.unpack_from("<III", hdr, 92)
        f.seek(base + hdr_size)
        tables = f.read(tables_size)
        for i in range(p_num):
            po = p_off + i * p_ent
            name = tables[po:po + 36].split(b"\x00", 1)[0].decode("ascii", "ignore")
            if name == target:
                first_ext, num_ext = struct.unpack_from("<II", tables, po + 40)
                if num_ext >= 1:
                    eo = e_off + first_ext * e_ent
                    num_sec, target_type, target_data = struct.unpack_from("<QIQ", tables, eo)
                    if target_type == 1:  # LP_TARGET_TYPE_LINEAR
                        print(target_data * 512, num_sec * 512)
                        sys.exit(0)
sys.exit(1)
PYEOF
}

mount_super_logical_ro() {
    local pname="$1"
    local mnt="$MNT_BASE/$pname"
    if mount_ro "$pname" "auto" 2>/dev/null; then
        return 0
    fi
    local super_dev
    super_dev=$(find_part_dev "super" || true)
    [ -n "$super_dev" ] || return 1
    local ext
    ext=$(find_super_extent "$super_dev" "$pname" 2>/dev/null || true)
    [ -n "$ext" ] || return 1
    local off sz lo
    off=$(echo "$ext" | awk '{print $1}')
    sz=$(echo "$ext" | awk '{print $2}')
    lo=$(losetup -f --show -r -o "$off" --sizelimit "$sz" "$super_dev" 2>/dev/null || true)
    [ -n "$lo" ] || return 1
    LOOP_DEVS+=("$lo")
    mkdir -p "$mnt"
    if mount -t erofs -o ro "$lo" "$mnt" 2>/dev/null || mount -o ro "$lo" "$mnt" 2>/dev/null; then
        LAST_MOUNT_DIR="$mnt"
        echo "[OK] 从 super 只读挂载逻辑分区 $pname ($lo)"
        return 0
    fi
    return 1
}

mount_super_slot_ro() {
    local base="$1"
    local s
    for s in "$SLOT" "$ALT_SLOT"; do
        if mount_super_logical_ro "${base}_${s}"; then
            return 0
        fi
    done
    echo "[WARN] 未能从 super 挂载逻辑分区 ${base}_${SLOT}/${ALT_SLOT}，跳过"
    return 1
}

echo "=== [1/5] 从本机 UFS 只读提取设备签名固件 (优先槽位 _${SLOT}，回退 _${ALT_SLOT}) ==="

# 1A. modem_{a,b}: Wi-Fi 7 (WCN7860), ADSP, CDSP, 5G Modem, IPA v5.5, tqftpserv files
if mount_slot_ro "modem" "vfat"; then
    M_ROOT="$LAST_MOUNT_DIR"
    IMG_DIR="$M_ROOT/image"
    [ -d "$IMG_DIR" ] || IMG_DIR="$M_ROOT"

    stage_wifi() {
        local dst="$1"; shift
        [ -s "$FW_WIFI/$dst" ] && return 0
        for pat in "$@"; do
            local f
            f=$(find "$M_ROOT" -maxdepth 4 -iname "$pat" -type f 2>/dev/null | head -n 1 || true)
            if [ -n "$f" ]; then
                cp -f "$f" "$FW_WIFI/$dst"
                echo "  [WiFi] $dst <- $(basename "$f")"
                return 0
            fi
        done
        echo "  [WARN] 未找到 $dst (尝试: $*)"
        return 0
    }
    stage_wifi "amss.bin" "amss20.bin" "amss.bin"
    stage_wifi "phy_ucode20.elf" "phy_ucode20.elf" "phy_ucode*.elf"
    stage_wifi "tmel_peach_20.elf" "tmel_peach_20.elf" "tmel_peach*.elf"
    stage_wifi "aux_ucode.bin" "aux_ucode20.elf" "aux_ucode.elf" "aux_ucode.bin" "aux_uc*.elf"
    stage_wifi "board.bin" "bdwlan.elf" "bdwlan.e*" "board.bin"
    stage_wifi "regdb.bin" "regdb.bin"
    stage_wifi "qdss_trace_config_v2.cfg" "qdss_trace_config_v2.cfg" "qdss_trace_config*.cfg"

    # PIL-squash split .mdt+.bXX into monolithic .mbn expected by sm8750-realme-pista.dts
    for sub in adsp adsp_dtb cdsp cdsp_dtb modem modem_dtb ipa_fws; do
        if [ -f "$IMG_DIR/${sub}.mbn" ]; then
            cp -f "$IMG_DIR/${sub}.mbn" "$FW_PISTA/${sub}.mbn" || true
        elif [ -f "$IMG_DIR/${sub}.mdt" ]; then
            pil_squash "$IMG_DIR/${sub}.mdt" "$FW_PISTA/${sub}.mbn" || true
        fi
    done

    # Copy tqftpserv / PD JSON / modem_pr / modem_oem / pakala files
    for d in modem_pr modem_oem pakala; do
        if [ -d "$IMG_DIR/$d" ]; then
            cp -af "$IMG_DIR/$d" "$FW_PISTA/" 2>/dev/null || true
        fi
    done
    cp -f "$IMG_DIR"/*.jsn "$FW_PISTA/" 2>/dev/null || true

    # GPU ZAP fallback if present in modem
    ZAP=$(find "$M_ROOT" -maxdepth 4 \( -iname "gen80000_zap.mbn" -o -iname "gen80000_zap.mdt" \) -type f 2>/dev/null | head -n 1 || true)
    if [ -n "$ZAP" ]; then
        if [[ "$ZAP" == *.mdt ]]; then
            pil_squash "$ZAP" "$FW_QCOM/gen80000_zap.mbn" || true
        else
            cp -f "$ZAP" "$FW_QCOM/gen80000_zap.mbn" || true
        fi
    fi
fi

# Prevent upstream linux-firmware archives (board-2.bin*, *.zst/*.xz) from shadowing device WCN7860 blobs
if [ -s "$FW_WIFI/amss.bin" ] && [ -s "$FW_WIFI/board.bin" ]; then
    rm -f "$FW_WIFI"/board-2.bin* "$FW_WIFI"/firmware-2.bin* \
          "$FW_WIFI"/amss.bin.zst "$FW_WIFI"/amss.bin.xz \
          "$FW_WIFI"/board.bin.zst "$FW_WIFI"/board.bin.xz \
          "$FW_WIFI"/regdb.bin.zst "$FW_WIFI"/regdb.bin.xz \
          "$FW_WIFI"/phy_ucode20.elf.zst "$FW_WIFI"/phy_ucode20.elf.xz \
          "$FW_WIFI"/tmel_peach_20.elf.zst "$FW_WIFI"/tmel_peach_20.elf.xz \
          "$FW_WIFI"/aux_ucode.bin.zst "$FW_WIFI"/aux_ucode.bin.xz 2>/dev/null || true
fi

# 1B. bluetooth_{a,b}: WCN7860 / Ganges 2.0 Bluetooth 6.0
if mount_slot_ro "bluetooth" "vfat"; then
    BT_ROOT="$LAST_MOUNT_DIR"
    find "$BT_ROOT" -maxdepth 3 \( -iname "gngbtfw20*" -o -iname "gngbtnv20*" \) -type f -exec cp -f {} "$FW_BT/" \; 2>/dev/null || true
    echo "  [BT] 已提取 Ganges 2.0 固件到 $FW_BT"
fi

# 1C. dsp_{a,b}: CDSP NPU unsigned shell + ADSP sensor libraries
if mount_slot_ro "dsp" "auto"; then
    D_ROOT="$LAST_MOUNT_DIR"
    [ -d "$D_ROOT/cdsp" ] && { cp -af "$D_ROOT/cdsp/." "$CDSP_DIR/" 2>/dev/null || true; }
    [ -d "$D_ROOT/adsp" ] && { cp -af "$D_ROOT/adsp/." "$SENS_DIR/dsp/adsp/" 2>/dev/null || true; }
fi

# 1D. super -> vendor_{a,b} & odm_{a,b} (EROFS read-only): GPU ZAP, Iris VPU 3.5, Sensor configs
if mount_super_slot_ro "vendor"; then
    V_ROOT="$LAST_MOUNT_DIR"
    for z in "$V_ROOT/firmware/gen80000_zap.mbn" "$V_ROOT/firmware/gen80000_zap.mdt"; do
        if [ -f "$z" ] && [ ! -s "$FW_QCOM/gen80000_zap.mbn" ]; then
            if [[ "$z" == *.mdt ]]; then
                pil_squash "$z" "$FW_QCOM/gen80000_zap.mbn" || true
            else
                cp -f "$z" "$FW_QCOM/gen80000_zap.mbn" || true
            fi
            echo "  [GPU] gen80000_zap.mbn <- $z"
        fi
    done
    VPU=$(find "$V_ROOT" -maxdepth 3 -iname "vpu35_4v.mbn" -type f 2>/dev/null | head -n 1 || true)
    if [ -n "$VPU" ]; then
        cp -f "$VPU" "$FW_PISTA/vpu35_4v.mbn" || true
        echo "  [VPU] vpu35_4v.mbn <- $VPU"
    fi
    [ -d "$V_ROOT/etc/sensors/config" ] && { cp -af "$V_ROOT/etc/sensors/config/." "$SENS_DIR/sensors/config/" 2>/dev/null || true; }
    [ -f "$V_ROOT/etc/sensors/sns_reg_config" ] && { cp -f "$V_ROOT/etc/sensors/sns_reg_config" "$SENS_DIR/sensors/sns_reg.conf" 2>/dev/null || true; }
fi

if mount_super_slot_ro "odm"; then
    O_ROOT="$LAST_MOUNT_DIR"
    [ -d "$O_ROOT/etc/sensor/config" ] && { cp -af "$O_ROOT/etc/sensor/config/." "$SENS_DIR/sensors/config/" 2>/dev/null || true; }
    [ -d "$O_ROOT/etc/sensors/config" ] && { cp -af "$O_ROOT/etc/sensors/config/." "$SENS_DIR/sensors/config/" 2>/dev/null || true; }
fi

# 1E. persist: Sensor calibration registry
if mount_ro "persist" "auto"; then
    [ -d "$LAST_MOUNT_DIR/sensors/registry/registry" ] && \
        { cp -af "$LAST_MOUNT_DIR/sensors/registry/registry/." "$SENS_DIR/sensors/registry/" 2>/dev/null || true; }
fi
printf "MTP\n" > "$SENS_DIR/socinfo/hw_platform"
printf "618\n" > "$SENS_DIR/socinfo/soc_id"
printf "2.0\n" > "$SENS_DIR/socinfo/revision"

echo "=== [2/5] 复制 5G 基带只读 EFS/NV 沙盒到 $RMTFS_DIR (绝不写 UFS) ==="
copy_raw_part() {
    local part="$1" dst="$2"
    [ -s "$RMTFS_DIR/$dst" ] && return 0
    local dev
    dev=$(find_part_dev "$part" || true)
    if [ -n "$dev" ]; then
        dd if="$dev" of="$RMTFS_DIR/$dst" bs=1M status=none 2>/dev/null && \
            { echo "  [RMTFS] $part -> $RMTFS_DIR/$dst"; return 0; }
    fi
    return 1
}

copy_slot_raw_part() {
    local base="$1" dst="$2"
    [ -s "$RMTFS_DIR/$dst" ] && return 0
    local s
    for s in "$SLOT" "$ALT_SLOT"; do
        if copy_raw_part "${base}_${s}" "$dst"; then
            return 0
        fi
    done
    copy_raw_part "$base" "$dst" || true
}

copy_raw_part "modemst1" "modemst1" || true
copy_raw_part "modemst2" "modemst2" || true
copy_raw_part "fsg" "fsg" || true
copy_raw_part "fsc" "fsc" || true
copy_slot_raw_part "oplusstanvbk" "oem_stanvbk" || true
copy_raw_part "oplusdycnvbk" "oem_dycnvbk" || true
ln -sf modemst1 "$RMTFS_DIR/modem_fs1"
ln -sf modemst2 "$RMTFS_DIR/modem_fs2"
ln -sf fsg      "$RMTFS_DIR/modem_fsg"
ln -sf fsc      "$RMTFS_DIR/modem_fsc"

echo "=== [3/5] 部署 /opt/pista-linux 离线硬件配置与服务 ==="
PISTA_DIR="/opt/pista-linux"
if [ -d "$PISTA_DIR" ]; then
    # Camera static files (online apt/dnf packages & gcc build handled by pista-install-pkgs)
    if [ -d "$PISTA_DIR/camera" ]; then
        install -d /usr/local/share/pista-camera/ipa/simple /usr/share/libcamera/ipa/simple /usr/local/lib /usr/share/applications
        install -m 0755 "$PISTA_DIR/camera/pista-camera" /usr/local/bin/pista-camera
        install -m 0644 "$PISTA_DIR/camera/pista_photo_isp.py" /usr/local/share/pista-camera/pista_photo_isp.py
        install -m 0644 "$PISTA_DIR/camera/pista_isp_profiles.json" /usr/local/share/pista-camera/pista_isp_profiles.json
        if [ -f "$PISTA_DIR/camera/pista_ae_guard.c" ]; then
            install -m 0644 "$PISTA_DIR/camera/pista_ae_guard.c" /usr/local/share/pista-camera/pista_ae_guard.c || true
        fi
        install -m 0644 "$PISTA_DIR/camera/ipa/"*.yaml /usr/share/libcamera/ipa/simple/ 2>/dev/null || true
        install -m 0644 "$PISTA_DIR/camera/ipa_pista/simple/"*.yaml /usr/local/share/pista-camera/ipa/simple/ 2>/dev/null || true
        install -m 0644 "$PISTA_DIR/camera/pista-camera.desktop" /usr/share/applications/pista-camera.desktop 2>/dev/null || true
        install -m 0644 "$PISTA_DIR/camera/99-pista-camera.rules" /etc/udev/rules.d/99-pista-camera.rules 2>/dev/null || true
    fi

    # Audio UCM2 + compiled open-source topology
    if [ -d "$PISTA_DIR/audio/ucm2/Qualcomm/sm8750/pista" ]; then
        mkdir -p /usr/share/alsa/ucm2/Qualcomm/sm8750/pista /usr/share/alsa/ucm2/conf.d/sm8750 /lib/firmware/qcom/sm8750
        cp -af "$PISTA_DIR/audio/ucm2/Qualcomm/sm8750/pista/." /usr/share/alsa/ucm2/Qualcomm/sm8750/pista/
        ln -sf ../../Qualcomm/sm8750/pista/Realme-pista.conf \
               /usr/share/alsa/ucm2/conf.d/sm8750/Realme-GT7Pro-pista-RE6018L1.conf
        if [ -f "$PISTA_DIR/audio/Realme-pista-tplg.bin" ]; then
            install -m 0644 "$PISTA_DIR/audio/Realme-pista-tplg.bin" /lib/firmware/qcom/sm8750/Realme-pista-tplg.bin || true
        fi
    fi

    # Bluetooth BDADDR
    if [ -f "$PISTA_DIR/bluetooth/pista-bt-bdaddr.sh" ]; then
        install -m 0755 "$PISTA_DIR/bluetooth/pista-bt-bdaddr.sh" /usr/local/sbin/pista-bt-bdaddr.sh
        install -m 0644 "$PISTA_DIR/bluetooth/pista-bt-bdaddr.service" /etc/systemd/system/pista-bt-bdaddr.service
        install -m 0644 "$PISTA_DIR/bluetooth/99-pista-bt-bdaddr.rules" /etc/udev/rules.d/99-pista-bt-bdaddr.rules
    fi

    # IR Blaster & Flash LED
    if [ -f "$PISTA_DIR/ir/99-pista-hw.rules" ]; then
        install -m 0644 "$PISTA_DIR/ir/99-pista-hw.rules" /etc/udev/rules.d/99-pista-hw.rules
        install -m 0755 "$PISTA_DIR/ir/pista-remote.py" /usr/local/bin/pista-remote
        if [ -f "$PISTA_DIR/ir/pista-remote.desktop" ]; then
            install -m 0644 "$PISTA_DIR/ir/pista-remote.desktop" /usr/share/applications/pista-remote.desktop || true
        fi
    fi

    # Sensors (hexagonrpcd + iio-sensor-proxy)
    if [ -d "$PISTA_DIR/sensors" ]; then
        install -m 0644 "$PISTA_DIR/sensors/79-pista-sensors.rules" /etc/udev/rules.d/79-pista-sensors.rules
        mkdir -p /etc/systemd/system/iio-sensor-proxy.service.d
        install -m 0644 "$PISTA_DIR/sensors/iio-sensor-proxy-override.conf" /etc/systemd/system/iio-sensor-proxy.service.d/override.conf
        # Only place distro package drop-in when standalone local /etc/systemd/system/hexagonrpcd.service isn't installed
        if [ ! -f /etc/systemd/system/hexagonrpcd.service ] && [ -f "$PISTA_DIR/sensors/hexagonrpcd-override.conf" ]; then
            mkdir -p /etc/systemd/system/hexagonrpcd.service.d
            install -m 0644 "$PISTA_DIR/sensors/hexagonrpcd-override.conf" /etc/systemd/system/hexagonrpcd.service.d/override.conf || true
        fi
    fi

    # 5G Modem (RAM-only rmtfs + pista-cellular)
    if [ -d "$PISTA_DIR/modem" ]; then
        mkdir -p /etc/systemd/system/rmtfs.service.d /etc/sudoers.d
        install -m 0644 "$PISTA_DIR/modem/rmtfs-override.conf" /etc/systemd/system/rmtfs.service.d/override.conf
        install -m 0755 "$PISTA_DIR/modem/pista-cellular.py" /usr/local/bin/pista-cellular
        install -m 0755 "$PISTA_DIR/modem/pista-cellular-gui.py" /usr/local/bin/pista-cellular-gui
        install -m 0644 "$PISTA_DIR/modem/pista-cellular.service" /etc/systemd/system/pista-cellular.service
        install -m 0644 "$PISTA_DIR/modem/pista-cellular.desktop" /usr/share/applications/pista-cellular.desktop
        install -m 0440 "$PISTA_DIR/modem/pista-cellular.sudoers" /etc/sudoers.d/pista-cellular
    fi
fi

# Persist autoload module list (especially alias-less protocols/subsystems: rfcomm, bnep, hidp, uhid, rmnet, typec_ucsi)
mkdir -p /etc/modules-load.d
cat > /etc/modules-load.d/pista.conf << 'EOF'
panel-realme-ab849
msm
qcom_pd_mapper
qcom_q6v5_pas
fastrpc
typec_ucsi
ucsi_glink
leds-qcom-flash
qcom-iris
ir-spi
bluetooth
btqca
hci_uart
rfcomm
bnep
hidp
uhid
pci-pwrctrl-pwrseq
ath12k_wifi7
qcom_battmgr
ipa
rmnet
EOF
chmod 0644 /etc/modules-load.d/pista.conf

echo "=== [4/5] 加载核心内核模块并刷新 udev / systemd ==="
PISTA_MODS="panel-realme-ab849 msm qcom_pd_mapper qcom_q6v5_pas fastrpc typec_ucsi ucsi_glink leds-qcom-flash qcom-iris ir-spi bluetooth btqca hci_uart rfcomm bnep hidp uhid pci-pwrctrl-pwrseq ath12k_wifi7 qcom_battmgr ipa rmnet"
if [ "${PISTA_EARLY_BOOT:-0}" = "1" ]; then
    echo "  [EARLY-BOOT] 固件已就绪，交由 systemd-modules-load 与 systemd-udev-trigger 统一加载驱动"
else
    if [ -s "$FW_WIFI/amss.bin" ] && [ ! -d /sys/class/net/wlan0 ]; then
        if [ -d /sys/module/ath12k_wifi7 ]; then
            modprobe -r ath12k_wifi7 ath12k 2>/dev/null || true
            sleep 0.5
        fi
        modprobe pci-pwrctrl-pwrseq 2>/dev/null || true
        sleep 0.6
        modprobe ath12k_wifi7 2>/dev/null || true
    fi
    for mod in $PISTA_MODS; do
        modprobe "$mod" 2>/dev/null || true
    done
    systemctl daemon-reload 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
fi

echo "=== [5/5] 离线固件与硬件配置补齐完成！ ==="
echo "提示：联网后系统将自动运行 pista-install-pkgs 安装基带/音频/相机用户态软件包（也可手动运行 sudo pista-install-pkgs）。"
