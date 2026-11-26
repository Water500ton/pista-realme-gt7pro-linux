# Mainline Linux 7.3.6 for Realme GT7 Pro (`pista` / RMX5010)

Mainline **Linux 7.3.6 (ARM64)** kernel bring-up, device tree, drivers, and boot tooling for the **Realme GT7 Pro (`pista`, RMX5010, Qualcomm Snapdragon 8 Elite / SM8750 / Pakala)**, designed to boot via the companion single-slot VAB-safe UEFI ([pista-realme-gt7pro-uefi](https://github.com/Water500ton/pista-realme-gt7pro-uefi)).

All features listed below as working have been verified on real Realme GT7 Pro hardware running **Debian GNU/Linux (`forky/sid`) + KDE Plasma 6 (Wayland / KWin) + Mesa 26** over Wi-Fi 7 iSCSI.

---

## Hardware Status

| Subsystem | Component | Status | Notes |
| --- | --- | --- | --- |
| **SoC / CPU** | Snapdragon 8 Elite (8x Oryon, SM8750) | Working | All 8 cores online, 14.2 GiB usable RAM, SCMI DVFS (`scmi-cpufreq` 3.53 GHz / 4.09+ GHz), `qcom-tsens` thermal |
| **Display (Native KMS)** | DPU 12.0 + DSI0 + Samsung Eco² OLED Plus `AB849_P_1_A0022` | Working | **1264×2780 @ 120Hz**, DSC 1.2 (10bpc→8bpp, 2 slices/pkt), zero-copy with Adreno 830, native MIPI DCS backlight; BOE `AB849_P_3` selectable via `panel_realme_ab849.boe_p3=1` |
| **GPU** | Adreno 830 (`0x44050000`, `msm`) | Working | GMU v5.1.25 + Mesa 26 Freedreno/Turnip, devfreq up to 1100 MHz (`simple_ondemand` / `performance`), thermal cooling (requires user's own device-signed `gen80000_zap.mbn`) |
| **Touchscreen** | Synaptics S3910 (`syna_tcm2` on SPI4) | Working | Multi-touch verified in Weston & KDE Plasma 6 |
| **Wi-Fi 7** | Qualcomm FastConnect 7900 / WCN7860 (`peach`, PCI `17cb:110e`) | Working | `ath12k_wifi7`, runtime read-only firmware staging from device `modem_a` |
| **USB** | M31 eUSB2 + DWC3 Gadget (CDC NCM / ECM / RNDIS) | Working | `usb0` (`172.16.42.1/24`) SSH & DHCP |
| **Storage** | UFS (`ufshcd-qcom`) + iSCSI (`iscsi_tcp`) | Working | Read-only stock partition access + remote iSCSI rootfs |
| **Keys / RTC** | PMK8550 PON Power + Vol+ (`pon_pwrkey` / `pon_resin`), PM8550 GPIO6 Vol- (`gpio-keys`), PM8xxx RTC | Working | Power, Volume Up, and Volume Down all verified in KDE Plasma 6 (`POWER_RESET_QCOM_PON` + `KEYBOARD_GPIO`) |
| **Audio (Speakers & Dual Mic)** | ADSP AudioReach (`q6apm`), Secondary MI2S (`SD1`) + Dual NXP `TFA9873` (`0x34`/`0x35`), SoundWire `WCD9395` (`AMIC1` + `AMIC5`) | Working | Stereo speakers & dual internal microphones (stereo 48 kHz capture + full-duplex) verified via ALSA & PipeWire / KDE Plasma 6 (`Built-in Audio Stereo Speakers` / `Internal Microphones`), using in-kernel `qcom_pd_mapper`, `Realme-pista.m4` topology, UCM2, and user's own device-signed `adsp.mbn`/`adsp_dtb.mbn` |
| **Bluetooth 6.0** | Qualcomm FastConnect 7900 / WCN7860 (`Ganges 2.0`, `0x40210200`) on `uart14` @ 8 Mbps | Working | `btqca` + `hci_uart` (`patches/0008` PeriSS baudrate `0x31`/`0x34` + `gngbtfw20.mbn`/`gngbtnv20.bin`), BlueZ 5.88 + KDE Plasma 6 `bluedevil` + PipeWire `libspa-0.2-bluetooth` (Classic/BLE/A2DP/HFP/LE Audio/HIDP/UHID), real device scan verified |
| **Battery / Charging** | ADSP `charger_pd` (`pmic_glink` + `qcom_battmgr`) + Oplus hardware fuel gauge (`OEM_READ_BUFFER`) | Working | Real SOC %, 2S 6500 mAh (~25.2 Wh) capacity, hardware SOH health %, cycle count, temperature, charging/discharging hotplug notifications, and **18W USB-PD (9V/2A)** fast charging verified in UPower & KDE Plasma 6 |
| **Modem / Camera** | WWAN, CamSS | WIP | Not yet enabled |

---

## Repository Layout

```text
├── configs/
│   └── pista_defconfig                                        # Minimal savedefconfig for Linux 7.3.6
├── dts/qcom/
│   └── sm8750-realme-pista.dts                                # Standalone readable copy of board DTS
├── drivers/
│   ├── gpu/drm/panel/panel-realme-ab849.c                     # AB849 1264x2780 120Hz DSC OLED panel driver
│   └── input/touchscreen/syna_tcm2{,.h,_spi.c}                # Synaptics S3910 TCM2 SPI touchscreen driver
├── sound/
│   └── soc/codecs/tfa9874.c                                   # Dual NXP TFA9873/TFA9874 Class-D speaker amp driver
├── audio/
│   ├── topology/Realme-pista.m4                               # AudioReach Secondary MI2S (SD1) + TX3 WCD9395 topology source
│   └── ucm2/Qualcomm/sm8750/pista/{Realme-pista,HiFi}.conf    # ALSA UCM2 profile (Speakers + Dual Mic) for PipeWire / KDE Plasma 6
├── bluetooth/
│   ├── pista-bt-bdaddr.sh                                     # Deterministic persistent BDADDR helper (from /etc/machine-id)
│   ├── pista-bt-bdaddr.service                                # Systemd oneshot unit for automatic public-addr config
│   └── 99-pista-bt-bdaddr.rules                               # Udev trigger on hci0 add
├── patches/                                                   # Clean patch series against upstream Linux 7.3.6
│   ├── 0001-arm64-dts-qcom-sm8750-add-realme-pista-and-adreno830.patch
│   ├── 0002-drm-msm-sm8750-adreno830-dsc-and-qtb500-fixes.patch
│   ├── 0003-drm-panel-add-realme-ab849-dsc-oled-panel-driver.patch
│   ├── 0004-input-touchscreen-add-synaptics-tcm2-s3910-spi-driver.patch
│   ├── 0005-wifi-ath12k-pwrseq-add-wcn7860-peach-support.patch
│   ├── 0006-asoc-codecs-add-nxp-tfa9873-tfa9874-speaker-amp-driver.patch
│   ├── 0007-power-supply-qcom_battmgr-add-realme-pista-gauge-and-pd18w.patch
│   └── 0008-bluetooth-btqca-hci_qca-add-ganges-2.0-wcn7860-support.patch
├── boot/
│   ├── cmdline.txt                                            # UEFI boot kernel command line
│   ├── init                                                   # Initramfs init (USB NCM + Wi-Fi 7 + iSCSI switch_root)
│   ├── pista-wifi.sh                                          # Read-only WCN7860 firmware stager from phone modem_a
│   ├── pista-iscsi.conf.example                               # Template for Wi-Fi / iSCSI settings
│   └── pack_esp_fat16.py                                      # Builds 100 MiB UFS-4K-native FAT16 ESP for recovery_b
├── scripts/
│   ├── apply_patches.sh                                       # Applies patches/0001..0008 to Linux 7.3.6
│   └── build_kernel.sh                                        # Builds ARM64 Image.efi, pista.dtb, and modules
└── tools/
    ├── pista-kvm-recv.py                                      # Phone uinput USB gaming keyboard/mouse receiver
    └── pista-kvm-send.py                                      # Host evdev -> TCP sender (ScrollLock / RCtrl+RAlt)
```

---

## Key Technical Fixes Included

1. **SM8750 QTB500 Warm-Reset Workaround (`msm_mdss.c`)**: Reads `mdp_hf_qtb` (`0x017d0000`) on `msm_mdss_enable()` before the first DSI command-mode DMA fetch, preventing the SM8750 display SMMU TBU wedge (`cmd dma tx failed, ret=-110`) after warm `reboot`.
2. **DSC 1.2 10bpc→8bpp 2-Slices-Per-Packet (`dsi_host.c` + `panel-realme-ab849.c`)**: Uses `CMD_DST_FORMAT_RGB888` when DSC is active on an `RGB101010` panel, programs `DSI_COMMAND_COMPRESSION_MODE_CTRL2_STREAM0_SLICE_WIDTH(bytes_per_pkt)`, and enables `MIPI_DSI_MODE_DSC_ALL_SLICES_IN_PKT` so both 632px horizontal slices pack cleanly into one 1264-byte packet per line.
3. **Clean Shutdown on Wi-Fi iSCSI Root (`boot/init`)**: Launches `@wpa_supplicant` and `@iscsid` using systemd's `@` `argv[0]` Root Storage Daemon convention so `systemd-shutdown` never tears down Wi-Fi before remounting `/` read-only.

---

## Building from Upstream Linux 7.3.6

### Prerequisites
- `clang`, `lld`, `llvm-objcopy`, `llvm-strip` (`LLVM=1` ARM64 cross-toolchain)
- `bc`, `bison`, `flex`, `python3`, `patch`, `cpio`, `gzip`

### 1. Fetch, Patch, and Build
```bash
wget https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.6.tar.xz
tar -xf linux-7.3.6.tar.xz

./scripts/apply_patches.sh ./linux-7.3.6
./scripts/build_kernel.sh ./linux-7.3.6 ./build
```

Outputs are placed in `./build/`:
- `build/Image.efi`
- `build/pista.dtb`
- `build/modules/lib/modules/7.3.6/`

### 2. Firmware Requirements (Not Redistributed)
This repository intentionally contains **no proprietary firmware blobs**. You must provide firmware from your own device and upstream `linux-firmware`:

- **WCN7860 Wi-Fi 7**: `boot/pista-wifi.sh` automatically mounts your phone's own `modem_a` partition **read-only** at boot and stages `amss.bin`, `phy_ucode20.elf`, `tmel_peach_20.elf`, `aux_ucode.bin`, `board.bin`, and `regdb.bin` into `/lib/firmware/ath12k/WCN7860/hw2.0/`.
- **Adreno 830 GPU**:
  - `qcom/gen80000_sqe.fw`, `qcom/gen80000_gmu.bin`, `qcom/gen80000_aqe.fw` from upstream `linux-firmware`.
  - Device-signed `qcom/gen80000_zap.mbn` extracted read-only from your own GT7 Pro `vendor_a` image (`/vendor/firmware_mnt` or `super` -> `vendor_a` EROFS).
- **ADSP AudioReach, Stereo Speakers & Dual Microphones**:
  - Device-signed `qcom/sm8750/realme/pista/adsp.mbn` and `adsp_dtb.mbn` squashed from `adsp.mdt/.b*` and `adsp_dtb.mdt/.b*` inside your own GT7 Pro `modem_a` partition.
  - Compile `audio/topology/Realme-pista.m4` against `linux-msm/audioreach-topology` with `m4` + `alsatplg` (or `libatopology`) to `/lib/firmware/qcom/sm8750/Realme-pista-tplg.bin`.
  - Install `audio/ucm2/Qualcomm/sm8750/pista/{Realme-pista,HiFi}.conf` under `/usr/share/alsa/ucm2/Qualcomm/sm8750/pista/` and symlink `/usr/share/alsa/ucm2/conf.d/sm8750/Realme-GT7Pro-pista-RE6018L1.conf`.
- **WCN7860 / Ganges 2.0 Bluetooth 6.0**:
  - Extract `gngbtfw20.mbn` and `gngbtnv20.bin` (and any `gngbtnv20.b*`) read-only from your own GT7 Pro `bluetooth_a` partition (`/dev/disk/by-partlabel/bluetooth_a`, VFAT `image/`) into `/lib/firmware/qca/`.
  - Install `bluetooth/pista-bt-bdaddr.sh` -> `/usr/local/sbin/`, `bluetooth/pista-bt-bdaddr.service` -> `/etc/systemd/system/`, and `bluetooth/99-pista-bt-bdaddr.rules` -> `/etc/udev/rules.d/` to automatically configure a persistent public BD address on boot.

### 3. VAB-Safe Booting (Important Safety Notice)
The Realme GT7 Pro uses Android 15 Virtual A/B (VAB):
- **NEVER switch A/B slots** and **NEVER overwrite `recovery_a`**.
- Pack your 100 MiB UFS-4K-native FAT16 ESP (`\boot\IMAGE.EFI`, `\boot\INITRD.IMG`, `\boot\PISTA.DTB`, `\boot\CMDLINE.TXT`) using `boot/pack_esp_fat16.py` and flash it **only to the inactive `recovery_b` partition**:
  ```bash
  fastboot flash recovery_b esp_recovery_b.img
  fastboot boot Mu-pista-android.img
  ```

---

## Credits & References
- Upstream Linux kernel & `linux-msm` / Freedreno / `ath12k` contributors
- SM8750 OnePlus 13 (`dodge`) mainline bring-up references for Adreno 830 catalog, WCN7860 `ath12k`, and SM8750 QTB500 / RGB101010+DSC insights
- Companion UEFI project: [pista-realme-gt7pro-uefi](https://github.com/Water500ton/pista-realme-gt7pro-uefi)

## License
Kernel patches, device tree, and drivers are licensed under **GPL-2.0-only** unless otherwise noted in individual file headers.
