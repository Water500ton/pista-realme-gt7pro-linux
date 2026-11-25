#!/bin/sh
# Stage WCN7860 (peach, 17cb:110e) firmware from stock UFS modem_a (read-only)
# and load pci-pwrctrl-pwrseq + ath12k_wifi7.

MODDIR="/lib/modules/$(uname -r)"
FWDIR="/lib/firmware/ath12k/WCN7860/hw2.0"
mkdir -p "$FWDIR" /mnt/modem

log() {
    echo "WIFI: $*" > /dev/kmsg 2>/dev/null || true
    echo "WIFI: $*"
}

# 1. Load PCI power-sequencing first so WCN7860 PMU powers up and enumerates on PCIe0
for m in \
    "$MODDIR/kernel/drivers/pci/pwrctrl/pci-pwrctrl-pwrseq.ko" \
    "$MODDIR/kernel/drivers/soc/qcom/qmi_helpers.ko"; do
    [ -f "$m" ] && insmod "$m" 2>/dev/null || true
done
sleep 0.5

# Report enumerated PCI devices
for d in /sys/bus/pci/devices/*; do
    [ -e "$d/vendor" ] || continue
    log "PCI $(basename "$d"): $(cat "$d/vendor"):$(cat "$d/device")"
done

# 2. Find stock modem_a partition on UFS (read-only) to stage Peach firmware
MODEM_DEV=""
for u in /sys/block/sd*/sd*/uevent; do
    [ -r "$u" ] || continue
    if grep -q '^PARTNAME=modem_a$' "$u"; then
        DEVNAME=$(awk -F= '/^DEVNAME=/{print $2}' "$u")
        MODEM_DEV="/dev/$DEVNAME"
        break
    fi
done

if [ -n "$MODEM_DEV" ] && [ -b "$MODEM_DEV" ]; then
    log "Found modem_a at $MODEM_DEV (mounting read-only)"
    if mount -t vfat -o ro "$MODEM_DEV" /mnt/modem 2>/dev/null; then
        stage_one() {
            dst="$1"
            shift
            [ -s "$FWDIR/$dst" ] && return 0
            for pat in "$@"; do
                f=$(find /mnt/modem -maxdepth 4 -iname "$pat" 2>/dev/null | head -n 1)
                if [ -n "$f" ] && [ -f "$f" ]; then
                    cp "$f" "$FWDIR/$dst"
                    log "Staged $dst <- $f"
                    return 0
                fi
            done
            log "Missing $dst (searched: $*)"
            return 1
        }
        stage_one "amss.bin" "amss20.bin" "amss.bin"
        stage_one "phy_ucode20.elf" "phy_ucode20.elf" "phy_ucode*.elf"
        stage_one "tmel_peach_20.elf" "tmel_peach_20.elf" "tmel_peach*.elf"
        stage_one "aux_ucode.bin" "aux_ucode20.elf" "aux_ucode.elf" "aux_ucode.bin" "aux_uc*.elf"
        stage_one "board.bin" "bdwlan.elf" "bdwlan.e*" "board.bin"
        stage_one "regdb.bin" "regdb.bin"
        stage_one "qdss_trace_config_v2.cfg" "qdss_trace_config_v2.cfg" "qdss_trace_config*.cfg" || true

        # Also stage device-signed Adreno 830 ZAP shader if present in modem_a
        ZAP=$(find /mnt/modem -maxdepth 4 -iname "gen80000_zap.mbn" -o -iname "gen80000_zap.mdt" -o -iname "*zap*.mbn" 2>/dev/null | head -n 1)
        if [ -n "$ZAP" ] && [ -f "$ZAP" ]; then
            mkdir -p /lib/firmware/qcom
            cp "$ZAP" /lib/firmware/qcom/gen80000_zap.mbn
            log "Staged GPU ZAP <- $ZAP"
        fi
        umount /mnt/modem 2>/dev/null || true
    else
        log "Could not mount $MODEM_DEV as vfat"
    fi
else
    log "modem_a partition not found yet on UFS"
fi

# 3. Load ath12k + ath12k_wifi7 if firmware is present
if [ -s "$FWDIR/amss.bin" ]; then
    for m in \
        "$MODDIR/kernel/drivers/net/wireless/ath/ath12k/ath12k.ko" \
        "$MODDIR/kernel/drivers/net/wireless/ath/ath12k/wifi7/ath12k_wifi7.ko"; do
        [ -f "$m" ] && insmod "$m" 2>/dev/null || true
    done
    sleep 1
    ip -o link | grep -E "wlan|wl" | while read -r l; do
        log "IFACE: $l"
    done
else
    log "Skip loading ath12k (amss.bin not found in modem_a; check vendor_a/odm_a over SSH)"
fi
