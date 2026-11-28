# Realme GT7 Pro (`pista` / RMX5010) 主线 Linux 7.3.6

> **语言 / Language**: [English](README.md) | 简体中文

针对 **Realme GT7 Pro（`pista` / RMX5010，高通骁龙 8 至尊版 / SM8750 / Pakala）** 的主线 **Linux 7.3.6 (ARM64)** 内核移植、设备树、驱动及启动工具集，配合单槽免切槽 VAB 安全 UEFI 固件（[pista-realme-gt7pro-uefi](https://github.com/Water500ton/pista-realme-gt7pro-uefi)）进行引导。

下文标注为可用的所有功能特性均已在真实 Realme GT7 Pro 硬件上运行 **Debian GNU/Linux (`forky/sid`) + KDE Plasma 6 (Wayland / KWin) + Mesa 26**（基于 Wi-Fi 7 iSCSI 根文件系统）完成实机验证。

---

## 硬件支持状态

| 子系统 | 组件 | 状态 | 说明 |
| --- | --- | --- | --- |
| **SoC / CPU** | Snapdragon 8 Elite (8× Oryon, SM8750) | 可用 | 全部 8 核心在线，14.2 GiB 可用内存，SCMI DVFS（`scmi-cpufreq` 3.53 GHz / 4.09+ GHz），`qcom-tsens` 温控 |
| **显示（原生 KMS）** | DPU 12.0 + DSI0 + 三星 Eco² OLED Plus `AB849_P_1_A0022` | 可用 | **1264×2780 @ 120Hz**，DSC 1.2（10bpc→8bpp，2 slices/pkt），与 Adreno 830 零拷贝合成，原生 MIPI DCS 背光调节；京东方 `AB849_P_3` 面板可通过 `panel_realme_ab849.boe_p3=1` 启用 |
| **GPU** | Adreno 830 (`0x44050000`, `msm`) | 可用 | GMU v5.1.25 + Mesa 26 Freedreno/Turnip，devfreq 最高 1100 MHz（`simple_ondemand` / `performance`），支持温控降频（需使用从本人手机提取并已签名的 `gen80000_zap.mbn`） |
| **触控屏** | Synaptics S3910（SPI4 上的 `syna_tcm2`） | 可用 | 多点触控已在 Weston 与 KDE Plasma 6 中实机验证 |
| **Wi-Fi 7** | Qualcomm FastConnect 7900 / WCN7860（`peach`，PCI `17cb:110e`） | 可用 | `ath12k_wifi7`，开机时从设备自身的 `modem_a` 分区只读提取并暂存固件 |
| **USB（从机与主机 OTG）** | M31 eUSB2 + DWC3 OTG（`xHCI` + CDC NCM Gadget）+ `ucsi_glink` + Oplus 自动 5V VBUS 升压 | 可用 | Type-C 双角色切换（默认从机模式 `usb0` `172.16.42.1/24`，插入设备时自动切换为主机模式并通过 `qcom_battmgr` `usb_register_notify()` 在冷插座状态下触发 5.1V / 1.5A VBUS 升压；已通过 UAS 实测 500GB 2.5 英寸移动硬盘，速率 **43.3 MB/s**）。*硬件说明：GT7 Pro 的 Type-C 物理上仅支持 USB 2.0（原厂 DTB 已禁用 `usb_qmp_dp_phy`，无 USB 3.x / DP Alt-Mode）。* |
| **存储** | UFS (`ufshcd-qcom`) + iSCSI (`iscsi_tcp`) | 可用 | 支持只读访问原厂分区与远程 iSCSI 根文件系统 |
| **物理按键 / RTC** | PMK8550 PON 电源键与音量+（`pon_pwrkey` / `pon_resin`）、PM8550 GPIO6 音量-（`gpio-keys`）、PM8xxx RTC | 可用 | 电源键、音量+ 和音量- 均已在 KDE Plasma 6 实机验证（`POWER_RESET_QCOM_PON` + `KEYBOARD_GPIO`） |
| **音频（扬声器与双麦克风）** | ADSP AudioReach (`q6apm`)、Secondary MI2S (`SD1`) + 双颗 NXP `TFA9873` (`0x34`/`0x35`)、SoundWire `WCD9395` (`AMIC1` + `AMIC5`) | 可用 | 立体声双扬声器与双内置麦克风（48 kHz 立体声采集与全双工）已通过 ALSA 与 PipeWire / KDE Plasma 6 验证（`Built-in Audio Stereo Speakers` / `Internal Microphones`），使用内核态 `qcom_pd_mapper`、`Realme-pista.m4` 拓扑、UCM2 及用户本机已签名的 `adsp.mbn`/`adsp_dtb.mbn` |
| **蓝牙 6.0** | `uart14` @ 8 Mbps 上的 Qualcomm FastConnect 7900 / WCN7860（`Ganges 2.0`, `0x40210200`） | 可用 | `btqca` + `hci_uart`（`patches/0008` PeriSS 波特率 `0x31`/`0x34` + `gngbtfw20.mbn`/`gngbtnv20.bin`）、BlueZ 5.88 + KDE Plasma 6 `bluedevil` + PipeWire `libspa-0.2-bluetooth`（Classic/BLE/A2DP/HFP/LE Audio/HIDP/UHID），已实机验证真实设备扫描 |
| **电池 / 充电** | ADSP `charger_pd`（`pmic_glink` + `qcom_battmgr`）+ Oplus 硬件电量计（`OEM_READ_BUFFER`） | 可用 | 真实电量百分比、双电芯串联（2S）6500 mAh（约 25.2 Wh）容量、硬件 SOH 健康度、循环次数、温度、充放电热插拔事件、**18W USB-PD (9V/2A)** 快充以及冷插座自动 **5V OTG VBUS 升压**均已在 UPower 与 KDE Plasma 6 实机验证 |
| **闪光灯 / 手电筒 LED** | PM8550 Flash LED（`pm8550_flash`, `leds-qcom-flash`） | 可用 | 露出单路实装白光 LED 通道（`led-sources = <1>`）至 `/sys/class/leds/white:flash`（`0..255` 对应 `0..500 mA` 手电筒常亮，闪光灯最高 `1500 mA` / `1280 ms`） |
| **硬件视频编解码** | Qualcomm Iris VPU 3.5（`qcom,sm8750-iris` + `videocc`） | 可用 | `/dev/video0` 硬件解码（**H.264 / HEVC / VP9 / AV1**，NV12/P010/UBWC）及 `/dev/video1` 硬件编码（**H.264 / HEVC**）；已使用用户本机已签名的 `vpu35_4v.mbn` 完成端到端 V4L2 M2M 编码+解码回环验证 |
| **红外遥控** | QUPv3 `spi5`（`gpio53` MOSI）+ PM8550 `L9B`（`2.504V`）+ `ir-spi` | 可用 | `/dev/lirc0` 原始 38 kHz LIRC 红外发射器 + 触控友好型 GTK3 图形界面 [`ir/pista-remote.py`](ir/pista-remote.py)，预置已实测的小米电视 / 小米盒子遥控码并支持导入 Flipper-IRDB `.ir` 文件；已在真实小米电视上实机验证 |
| **传感器（ADSP SSC）** | `fastrpc`（`/dev/fastrpc-adsp-secure`）+ 打补丁的 `hexagonrpcd` + `libssc` + `iio-sensor-proxy` | 可用 | **加速度计**（`BMI26x`）、**距离传感器**（`TCS3720`）、**磁力计**（`AK09919`）和 **电子罗盘 / 旋转矢量**（`rotv`）通过 ADSP SSC 与 `iio-sensor-proxy` 实时数据流上报（`monitor-sensor` + KDE Plasma 6 自动旋转）；需从本机 `persist`/`vendor_a`/`odm_a`/`dsp_a` 分区只读暂存传感器注册表与配置文件 |
| **NPU（Hexagon V79 CDSP）** | `remoteproc_cdsp`（`qcom,sm8650-cdsp-pas`）+ `nsp_noc` + `fastrpc`（`/dev/fastrpc-cdsp`） | 可用 | Hexagon **V79**（`0x8c79`）、**6× 128-byte HVX** 向量引擎、**8 MiB VTCM** SRAM、9 个 SMMU 计算上下文以及 **Unsigned PD** 计算已通过 DMA-BUF（`fastrpc_shell_unsigned_3`）端到端实机验证；需用户本机已签名的 `cdsp.mbn`/`cdsp_dtb.mbn` + `dsp_a/cdsp/` |
| **5G 基带（MPSS + IPA v5.5）** | `remoteproc_mpss`（`qcom,sm8750-mpss-pas`）+ SM8750 **IPA v5.5**（`qcom,sm8750-ipa`, `rmnet_ipa0` + `rmnet.ko` QMAPv5）+ EFS 安全沙盒 `rmtfs` + `tqftpserv` | 可用 | **5G NR SA（`n78` 100 MHz）** 与 **4G LTE** 实网注册、**双物理 USIM 卡槽** 以及端到端 **蜂窝 IPv4 数据网络**（`ping` 延迟 **28.8 ms** RTT + 数 MB HTTP 下载，开启 QMAPv5 硬件聚合与校验和卸载，0 错误 / 0 丢包）已与激活状态的 Wi-Fi 7（`wlan0`）、ADSP、CDSP 并发实机验证。采用纯内存 EFS/NV 沙盒（`/var/lib/pista-rmtfs`，绝不写入 UFS EFS/NV 分区）。*说明：* `modem/pista-cellular.py` 负责干净的 DPM/WDA/WDS QMAPv5 拉起（上游 `ModemManager 1.24.2` 存在双卡双待误报 SIM 卡热插拔的 quirk）；QMI LOC 服务（`16 v2`）已在线（`standalone` / 引擎锁已解锁），SM8750 GNSS 事件注册仍处于 WIP 状态。 |
| **相机（全部 4 颗传感器 + 双闭环自动对焦 + 计算摄影 ISP）** | SM8750 **CAMSS**（`csid-980`, `vfe-gen4`, CSIPHY v2.3.0 D-PHY 4-lane 与 **原生 C-PHY 3-trio**）+ `camcc` + `cambistmclkcc` + `cci0`/`cci1` + PM8010M/N + **Sony IMX906**（50MP 主摄广角, C-PHY）+ **AK7377** 对焦马达、**Sony IMX882 / LYT-600**（50MP 3× 潜望长焦, C-PHY）+ **AK7316** 对焦马达、**Sony IMX355**（8MP 超广角, D-PHY）、**Sony IMX471**（16MP 前置, D-PHY） | 可用 | 全部 4 颗传感器均已通过 `v4l2-ctl`、**`libcamera v0.7.2` + Adreno 830 GPU SoftISP** 完成实机验证，配套 **逐传感器标定调优 YAML**（全 4 颗传感器启用 `BlackLevel` + 多色温 3×3 CCM，后置 **30 fps** / 前置 **58 fps**）、PipeWire（`libspa-0.2-libcamera`）以及 4 镜头触控友好型 GTK3 相机应用 [`camera/pista-camera`](camera/pista-camera)，提供 **原厂 EEPROM 标定的闭环自动对焦**（约 0.6s 流水线式 BAF 风格 CDAF/CAF + 点按对焦 + 手动对焦滑块）及完整的 **计算摄影 ISP**（[`camera/pista_photo_isp.py`](camera/pista_photo_isp.py)：**3 帧/6 帧 MFNR** 多帧对齐降噪及 FFT 对齐与运动拒绝、**3-EV 包围曝光拉普拉斯金字塔 Mertens HDR**、**单机原厂 17×13 EEPROM LSC 镜头阴影校正**、**局部色调映射 LTM + 胶片感 S 曲线**、**YCbCr 色噪抑制 + 无光晕多尺度锐化 ASF**，以及完整智能手机 EXIF）。无需任何专有固件。 |
| **振动马达 / NFC** | PMIH0108 高压线性马达、NXP NFC | WIP | 暂未启用 |

---

## 仓库目录结构

```text
├── configs/
│   └── pista_defconfig                                        # Linux 7.3.6 精简内核配置 (savedefconfig)
├── dts/qcom/
│   └── sm8750-realme-pista.dts                                # 独立可读的板级设备树 (DTS) 副本
├── drivers/
│   ├── gpu/drm/panel/panel-realme-ab849.c                     # AB849 1264x2780 120Hz DSC OLED 面板驱动
│   └── input/touchscreen/syna_tcm2{,.h,_spi.c}                # Synaptics S3910 TCM2 SPI 触控屏驱动
├── sound/
│   └── soc/codecs/tfa9874.c                                   # 双颗 NXP TFA9873/TFA9874 D 类扬声器功放驱动
├── audio/
│   ├── topology/Realme-pista.m4                               # AudioReach Secondary MI2S (SD1) + TX3 WCD9395 拓扑源码
│   └── ucm2/Qualcomm/sm8750/pista/{Realme-pista,HiFi}.conf    # 面向 PipeWire / KDE Plasma 6 的 ALSA UCM2 配置（扬声器 + 双麦克风）
├── bluetooth/
│   ├── pista-bt-bdaddr.sh                                     # 确定性固定蓝牙地址生成脚本（基于 /etc/machine-id）
│   ├── pista-bt-bdaddr.service                                # 启动时自动设置公共蓝牙地址的 systemd oneshot 服务
│   └── 99-pista-bt-bdaddr.rules                               # hci0 添加事件 udev 触发规则
├── ir/
│   ├── pista-remote.py                                        # 触控友好型 GTK3 红外遥控应用（内置小米电视预设 + Flipper .ir + 手电筒）
│   ├── pista-remote.desktop                                   # 桌面应用启动器入口
│   └── 99-pista-hw.rules                                      # /dev/lirc0 与 /sys/class/leds/white:flash 的 udev 权限规则
├── sensors/
│   ├── hexagonrpcd-pista.patch                                # 上游 hexagonrpc 补丁（/odm/etc/sensor/config 映射 + 扩展 RPC 非致命处理）
│   ├── 79-pista-sensors.rules                                 # 在 iio-sensor-proxy 中启用 ssc-accel/light/proximity/compass 的 udev 规则
│   ├── hexagonrpcd-override.conf                              # Systemd drop-in 覆盖配置（/dev/fastrpc-adsp-secure + /var/lib/pista-sensors/）
│   └── iio-sensor-proxy-override.conf                         # Systemd drop-in 覆盖配置（确保 iio-sensor-proxy 在 hexagonrpcd 之后启动）
├── modem/
│   ├── rmtfs-override.conf                                    # EFS 安全的纯内存沙盒 rmtfs systemd drop-in 配置（绝不写入 UFS EFS）
│   ├── pista-cellular.py                                      # 面向 rmnet_ipa0 的 QRTR/QMI DPM+WDA+WDS QMAPv5 蜂窝拨号守护进程与命令行工具
│   ├── pista-cellular-gui.py                                  # 面向 KDE Plasma 6 的触控友好型 GTK3 5G/4G 蜂窝网络控制界面
│   ├── pista-cellular.service                                 # 后台蜂窝会话 systemd 服务单元
│   ├── pista-cellular.desktop                                 # 桌面应用启动器（"5G 蜂窝网络"）
│   └── pista-cellular.sudoers                                 # 面向触控 GUI 开关的细粒度 NOPASSWD sudoers 规则
├── camera/
│   ├── pista-camera                                           # 触控友好型 GTK3 + GStreamer libcamera 相机应用（4 镜头 + AF + MFNR/HDR 模式）
│   ├── pista_photo_isp.py                                     # 计算摄影 ISP（MFNR、3-EV Mertens HDR、17x13 LSC、LTM、ANR/ASF、EXIF）
│   ├── pista_isp_profiles.json                                # 全 4 颗镜头的 17x13 RGB 黄金镜头阴影与光学元数据配置
│   ├── ipa/{imx906,imx882,imx355,imx471}.yaml                 # 已标定的 libcamera Simple SoftISP YAML（BlackLevel + 多色温 3x3 CCM）
│   ├── pista-camera.desktop                                   # 桌面应用启动器（"相机" / "Camera"）
│   ├── 99-pista-camera.rules                                  # /dev/dma_heap/* 与 /dev/udmabuf 的 udev 权限规则（非 root GPU SoftISP）
│   └── install.sh                                             # 安装 libcamera YAML、PipeWire SPA、pista-camera、ISP 引擎及 EEPROM 标定
├── patches/                                                   # 基于上游 Linux 7.3.6 的干净补丁系列
│   ├── 0001-arm64-dts-qcom-sm8750-add-realme-pista-and-adreno830.patch
│   ├── 0002-drm-msm-sm8750-adreno830-dsc-and-qtb500-fixes.patch
│   ├── 0003-drm-panel-add-realme-ab849-dsc-oled-panel-driver.patch
│   ├── 0004-input-touchscreen-add-synaptics-tcm2-s3910-spi-driver.patch
│   ├── 0005-wifi-ath12k-pwrseq-add-wcn7860-peach-support.patch
│   ├── 0006-asoc-codecs-add-nxp-tfa9873-tfa9874-speaker-amp-driver.patch
│   ├── 0007-power-supply-qcom_battmgr-add-realme-pista-gauge-and-pd18w.patch
│   ├── 0008-bluetooth-btqca-hci_qca-add-ganges-2.0-wcn7860-support.patch
│   ├── 0009-net-ipa-add-sm8750-ipa-v5.5-and-qmi-smem-tlv-support.patch
│   ├── 0010-media-camss-add-sm8750-camera-camcc-pinctrl-and-imx471-support.patch
│   ├── 0011-media-camss-i2c-add-sm8750-cphy-3trio-imx906-and-imx882-support.patch
│   └── 0012-media-i2c-ak7375-add-ak7377-and-ak7316-vcm-support.patch
├── boot/
│   ├── cmdline.txt                                            # UEFI 启动内核命令行参数
│   ├── init                                                   # Initramfs 启动脚本（USB NCM + Wi-Fi 7 + iSCSI switch_root）
│   ├── pista-wifi.sh                                          # 从手机 modem_a 只读暂存 WCN7860 固件的脚本
│   ├── pista-iscsi.conf.example                               # Wi-Fi / iSCSI 配置模板
│   └── pack_esp_fat16.py                                      # 生成适配 recovery_b 的 100 MiB UFS-4K 原生扇区 FAT16 ESP 镜像
├── scripts/
│   ├── apply_patches.sh                                       # 将 patches/0001..0012 应用到 Linux 7.3.6 源码树
│   └── build_kernel.sh                                        # 编译 ARM64 Image.efi、pista.dtb 与内核模块
└── tools/
    ├── pista-kvm-recv.py                                      # 手机端 uinput USB 游戏键盘/鼠标接收器
    └── pista-kvm-send.py                                      # 宿主机 evdev -> TCP 发送端（支持 ScrollLock / RCtrl+RAlt 切换）
```

---

## 核心技术修复与关键细节

1. **SM8750 QTB500 热重启卡死绕过（`msm_mdss.c`）**：在 `msm_mdss_enable()` 中首次 DSI 命令模式 DMA 传输前读取 `mdp_hf_qtb`（`0x017d0000`），防止热 `reboot` 后出现 SM8750 显示 SMMU TBU 锁死（`cmd dma tx failed, ret=-110`）。
2. **DSC 1.2 10bpc→8bpp 单包双分片（`dsi_host.c` + `panel-realme-ab849.c`）**：在 `RGB101010` 面板启用 DSC 时使用 `CMD_DST_FORMAT_RGB888`，编程配置 `DSI_COMMAND_COMPRESSION_MODE_CTRL2_STREAM0_SLICE_WIDTH(bytes_per_pkt)` 并启用 `MIPI_DSI_MODE_DSC_ALL_SLICES_IN_PKT`，使每行两个 632px 水平分片干净地打包进单个 1264 字节数据包。
3. **Wi-Fi iSCSI 根文件系统干净关机（`boot/init`）**：按照 systemd 的 `@` `argv[0]` 根存储守护进程规范启动 `@wpa_supplicant` 与 `@iscsid`，避免 `systemd-shutdown` 在将 `/` 重新挂载为只读之前提前断开 Wi-Fi。
4. **OBS Studio 预览与硬件编码技巧**：使用 `MESA_LOADER_DRIVER_OVERRIDE=zink obs`（Adreno 830 Turnip Vulkan 后端）启动 OBS 以消除 Wayland PipeWire UBWC 预览闪烁，并在自定义输出（FFmpeg）中选择 `h264_v4l2m2m` 或 `hevc_v4l2m2m`（`NV12`）启用 VPU 3.5 硬件编码。
5. **SM8750 IPA v5.5（`patches/0009` + `dts/qcom/sm8750-realme-pista.dts`）**：新增 SM8750 GSI 端点（`AP_LAN_RX` ch14/ep17、`AP_MODEM_RX` ch1/ep24、`MODEM_AP_RX` ch7/ep22）、SM8750 无重叠 IPA SRAM 分区，以及防止 MPSS 在握手时触发 `IPA_SMEM_ALLOC_EX` 断言所需的三个 SM8750 `QMI_IPA_INIT_MODEM_DRIVER_REQ` TLV（`0x23` `hw_filter_stats`、`0x24` `ipa_smem_info`、`0x25` `smem_info`，请求长度 197）。
6. **WCN7860 / MPSS 共享 SMPS 电源共存（`sm8750-realme-pista.dts`）**：提高 `vreg_s4d_0p85`/`vreg_s5f_0p85`（`876 mV`）、`vreg_s7i_1p2`（`1312 mV`）、`vreg_s3g_1p8`（`1864 mV` HPM）、`vreg_s1d_0p97`（`976 mV`）与 `vreg_l1f_0p88`（HPM）供电以匹配原厂 `wlan_peach` 需求，避免 MPSS 拉起射频时造成 `1c00000.pcie`（WCN7860 Wi-Fi 7）掉电欠压。
7. **SM8750 相机子系统与 TLMM I2C 强上拉（`patches/0010` + `sm8750-realme-pista.dts`）**：在 `pinctrl-sm8750.c`（`PINGROUP`）中新增 `.i2c_pull_bit = 13`，并在 `cci0_0_default`（`gpio113`/`gpio114`）配置 `bias-pull-up = <2200>`，确保 1 MHz CCI0 I2C0 与 IMX355 稳定通信；将 CSIPHY2 与 CSIPHY4 的 0.9V 供电映射到 `vreg_l3i_0p88`（其余 CSIPHY0/1/3/5 使用 `vreg_l1f_0p88`）；将 `cci1` 仅绑定至 `cci1_0`，避免与 `gpio164` 上的右扬声器功放复位脚冲突；为 `cci0`/`cci1` 设置 `37.5 MHz` 指定时钟；为 `imx471.c` 增加 OF 匹配、`vdig`/`vio` 电源及 `V4L2_SEL_TGT_CROP` 修复；并在 `camss.c` 中独立异步绑定各个传感器。
8. **原生 SM8750 MIPI C-PHY 3-Trio 与 PM8010N `smpd1` 1100 mV 上限修复（`patches/0011` + `sm8750-realme-pista.dts`）**：为 `qcom-camss` 增加完整的 3-trio MIPI C-PHY 支持（`V4L2_MBUS_CSI2_CPHY`、`bus-type = <1>`、`camss-csid-980.c` `CSI2_RX_CFG0_PHY_TYPE_SEL = BIT(24)`、SM8750 CSIPHY v2.3.0 3PH 的 `0x1000=0x01` 进入复位 / `0x108c=0x0e` / `0x1014=0x2a` / 1.5–2.0 Gsps CDR 与 settle 参数 / `0x1000=0x0e` 退出复位时序，以及 `csiphy_timer` 时钟速率限幅），并提供面向 Sony IMX906（50MP 主摄广角，`1634.4 Msps`）和 Sony IMX882 / LYT-600（50MP 3× 潜望长焦，`1632.0 Msps`）的干净 V4L2 子设备驱动。尤为关键的是：Realme GT7 Pro（`pista`）的 `PM8010N` `VIN_L1_L2` 在 AOP VRM 中父级供电为 `smpd1`（`vreg_s1d_0p97`，硬件上限 `1100 mV`）而非 `smpi7`；配置 `vdd-l1-l2-supply = <&vreg_s1d_0p97>` 并将 `vreg_l1n_1p1` 设为 `1096000 uV`（`pmic5_nldo502` 不超过 `1100 mV` 的精确 8 mV 步进档位），可彻底避免点亮 IMX882 `vdig` 时因越界触发 AOP `ERR_FATAL` 硬重启（`PS_HOLD`）（相比之下，SM8750 MTP 的 `1104000 uV` 超出 `smpd1` 1100 mV 上限 4 mV）。
9. **闭环 VCM 自动对焦（`patches/0012` + `camera/pista-camera`）**：在主线 `ak7375.c` 基础上扩展支持 `asahi-kasei,ak7377`（`cci0_i2c1 @ 0x0c`，`vreg_l3n_2p8` 2.8V，IMX906 主摄广角）及 `asahi-kasei,ak7316`（`cci1_i2c0 @ 0x2d`，`vreg_l7n_3p1` 3.1V，IMX882 3× 潜望长焦，包含设置模式解锁初始化 `0xae=0x3b -> 0x09=0x00 -> 0xae=0x00`、中心 `512` 归位与防震步进），并通过 `lens-focus` 关联以便 `libcamera` 发现。`pista-camera --calibrate-eeprom` 会从每台设备的 `GT24P128G` EEPROM（`0x50`，偏移 `0x0092..0x0097`）读取原厂近焦/无穷远/中焦 DAC 标定数据写入 `/etc/pista-camera-af.json`，驱动受高通 BAF 启发的双频带（Tenengrad + Modified Laplacian）两帧流水线粗/细抛物线自动对焦引擎，支持连续自动对焦（CAF）、点按对焦与标定手动对焦。

---

## 从上游 Linux 7.3.6 构建

### 构建环境依赖
- `clang`、`lld`、`llvm-objcopy`、`llvm-strip`（`LLVM=1` ARM64 交叉编译工具链）
- `bc`、`bison`、`flex`、`python3`、`patch`、`cpio`、`gzip`

### 1. 获取源码、打补丁与构建
```bash
wget https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.6.tar.xz
tar -xf linux-7.3.6.tar.xz

./scripts/apply_patches.sh ./linux-7.3.6
./scripts/build_kernel.sh ./linux-7.3.6 ./build
```

编译产物位于 `./build/` 目录：
- `build/Image.efi`
- `build/pista.dtb`
- `build/modules/lib/modules/7.3.6/`

### 2. 固件依赖说明（本仓库不分发专有固件）
本仓库刻意**不包含任何专有闭源固件**，你需要从自己的手机本机以及上游 `linux-firmware` 提取所需固件：

- **WCN7860 Wi-Fi 7**：`boot/pista-wifi.sh` 会在启动阶段自动以**只读**方式挂载手机本机的 `modem_a` 分区，并将 `amss.bin`、`phy_ucode20.elf`、`tmel_peach_20.elf`、`aux_ucode.bin`、`board.bin` 与 `regdb.bin` 暂存到 `/lib/firmware/ath12k/WCN7860/hw2.0/`。
- **Adreno 830 GPU**：
  - 从上游 `linux-firmware` 获取 `qcom/gen80000_sqe.fw`、`qcom/gen80000_gmu.bin`、`qcom/gen80000_aqe.fw`。
  - 从你自己 GT7 Pro 的 `vendor_a` 镜像（`/vendor/firmware_mnt` 或 `super` -> `vendor_a` EROFS）中只读提取已签名的 `qcom/gen80000_zap.mbn`。
- **ADSP AudioReach、立体声扬声器与双麦克风**：
  - 将你自己 GT7 Pro `modem_a` 分区内的 `adsp.mdt/.b*` 与 `adsp_dtb.mdt/.b*` 合并为设备签名的 `qcom/sm8750/realme/pista/adsp.mbn` 和 `adsp_dtb.mbn`。
  - 基于 `linux-msm/audioreach-topology` 使用 `m4` + `alsatplg`（或 `libatopology`）将 `audio/topology/Realme-pista.m4` 编译为 `/lib/firmware/qcom/sm8750/Realme-pista-tplg.bin`。
  - 将 `audio/ucm2/Qualcomm/sm8750/pista/{Realme-pista,HiFi}.conf` 安装到 `/usr/share/alsa/ucm2/Qualcomm/sm8750/pista/`，并建立符号链接 `/usr/share/alsa/ucm2/conf.d/sm8750/Realme-GT7Pro-pista-RE6018L1.conf`。
- **WCN7860 / Ganges 2.0 蓝牙 6.0**：
  - 从你自己 GT7 Pro 的 `bluetooth_a` 分区（`/dev/disk/by-partlabel/bluetooth_a`，VFAT `image/` 目录）只读提取 `gngbtfw20.mbn` 与 `gngbtnv20.bin`（及所有 `gngbtnv20.b*`）放入 `/lib/firmware/qca/`。
  - 将 `bluetooth/pista-bt-bdaddr.sh` 安装到 `/usr/local/sbin/`、`bluetooth/pista-bt-bdaddr.service` 安装到 `/etc/systemd/system/`、`bluetooth/99-pista-bt-bdaddr.rules` 安装到 `/etc/udev/rules.d/`，以在开机时自动配置持久化的公共蓝牙 MAC 地址。
- **Qualcomm Iris VPU 3.5 硬件视频编解码器**：
  - 从你自己 GT7 Pro 的 `vendor_a` 镜像（`super` -> `vendor_a` EROFS `/firmware/vpu35_4v.mbn`）中只读提取设备签名的 `vpu35_4v.mbn` 至 `/lib/firmware/qcom/sm8750/realme/pista/vpu35_4v.mbn`。
- **红外遥控与后置手电筒图形界面（`ir/pista-remote.py`）**：
  - 将 `ir/99-pista-hw.rules` 安装到 `/etc/udev/rules.d/`、`ir/pista-remote.py` 安装到 `/usr/local/bin/pista-remote`、`ir/pista-remote.desktop` 安装到 `/usr/share/applications/`（需安装 `python3-gi` / GTK3；加入 `video` 用户组后无需 root 权限）。
- **ADSP SSC 传感器（`sensors/`）**：
  - 从本机只读提取传感器文件到 `/var/lib/pista-sensors/`：`persist/sensors/registry/registry/*` -> `sensors/registry/`，`vendor_a/etc/sensors/config/*` + `odm_a/etc/sensor/config/*` -> `sensors/config/`，`vendor_a/etc/sensors/sns_reg_config` -> `sensors/sns_reg.conf`，`dsp_a/adsp/*` -> `dsp/adsp/`，并填充 `socinfo/{hw_platform=MTP,soc_id=618,revision=2.0}`。
  - 基于 `sensors/hexagonrpcd-pista.patch` 编译上游 `linux-msm/hexagonrpc`（增加 `/odm/etc/sensor/config` 虚拟路径映射，并在遇到 SM8750 扩展方法 ID 如 method 36 时保持 FastRPC 监听存活），随后安装 `sensors/79-pista-sensors.rules`、`sensors/hexagonrpcd-override.conf` 与 `sensors/iio-sensor-proxy-override.conf`。
- **Hexagon V79 NPU（CDSP）**：
  - 从你自己 GT7 Pro 的 `modem_a` 分区只读合并 `cdsp.mdt/.b*` 与 `cdsp_dtb.mdt/.b*` 为 `/lib/firmware/qcom/sm8750/realme/pista/cdsp.mbn` 和 `cdsp_dtb.mbn`，并将 `dsp_a/cdsp/*`（`fastrpc_shell_3`、`fastrpc_shell_unsigned_3` 等）提取至 `/usr/lib/qcom/cdsp/`。将用户加入 `fastrpc` 用户组即可非 root 访问 `/dev/fastrpc-cdsp`。
- **5G 调制解调器（MPSS + IPA v5.5 — EFS 安全沙盒）**：
  - 从 `modem_a` 只读合并 `modem.mdt/.b*`、`modem_dtb.mdt/.b*` 与 `ipa_fws.mdt/.b*` 到 `/lib/firmware/qcom/sm8750/realme/pista/`，拷贝 `modem_pr/`、`modem_oem/`、`pakala/`、`modemr.jsn` 供 `tqftpserv` 使用，并只读拷贝 `modemst1`、`modemst2`、`fsg`、`fsc`（以及 `oplusstanvbk_a` -> `oem_stanvbk` 和 `oplusdycnvbk` -> `oem_dycnvbk` 以实现完整射频在线校准）到 `/var/lib/pista-rmtfs/`（建立符号链接 `modem_fs1 -> modemst1`、`modem_fs2 -> modemst2`、`modem_fsg -> fsg`、`modem_fsc -> fsc`）。
  - 将 `modem/rmtfs-override.conf` 安装至 `/etc/systemd/system/rmtfs.service.d/override.conf`（通过 `rmtfs -o /var/lib/pista-rmtfs -r` 运行，绝不触碰 Android 的 UFS EFS/NV 分区）。
  - 安装 CLI/守护进程与触控友好型 GTK3 图形界面：`modem/pista-cellular.py` -> `/usr/local/bin/pista-cellular`、`modem/pista-cellular-gui.py` -> `/usr/local/bin/pista-cellular-gui`、`modem/pista-cellular.service` -> `/etc/systemd/system/`、`modem/pista-cellular.desktop` -> `/usr/share/applications/`，以及 `modem/pista-cellular.sudoers` -> `/etc/sudoers.d/pista-cellular`（`chmod 0440`）。从应用菜单启动 **5G 蜂窝网络**（或在终端运行 `sudo pista-cellular --default-route`），即可在不干扰 Wi-Fi / 局域网 iSCSI 路由的情况下自动启动 MPSS、开启 5G NR / 4G LTE 并在 `rmnet_ipa0` -> `rmnet_data0` 上拉起 QMAPv5 数据连接。
- **相机（全部 4 颗传感器：IMX906 + IMX882 + IMX355 + IMX471 — 零专有二进制固件）**：
  - 在手机端运行 `sudo ./camera/install.sh` 即可安装 `libcamera-tools`、`libspa-0.2-libcamera`（PipeWire 相机集成）、`gstreamer1.0-libcamera`、`gstreamer1.0-gtk3`、`/etc/udev/rules.d/99-pista-camera.rules`（允许非 root 用户访问 `/dev/dma_heap/system` 以启用 Adreno 830 GPU SoftISP）以及 4 镜头触控友好型竖屏相机应用 `/usr/local/bin/pista-camera`（**相机** / **Camera**）。

### 3. VAB 安全启动与刷写（重要安全提示）
Realme GT7 Pro 采用 Android 15 虚拟 A/B（VAB）分区架构：
- **切勿切换 A/B 槽位**，且**切勿覆盖 `recovery_a`**。
- 使用 `boot/pack_esp_fat16.py` 打包 100 MiB UFS-4K 原生扇区 FAT16 ESP 镜像（包含 `\boot\IMAGE.EFI`、`\boot\INITRD.IMG`、`\boot\PISTA.DTB`、`\boot\CMDLINE.TXT`），并**仅刷入非活动槽位的 `recovery_b` 分区**：
  ```bash
  fastboot flash recovery_b esp_recovery_b.img
  fastboot boot Mu-pista-android.img
  ```

---

## 致谢与参考
- 上游 Linux 内核及 `linux-msm` / Freedreno / `ath12k` 贡献者
- SM8750 OnePlus 13 (`dodge`) 主线化项目提供的 Adreno 830 catalog、WCN7860 `ath12k` 及 SM8750 QTB500 / RGB101010+DSC 技术参考
- 配套 UEFI 固件仓库：[pista-realme-gt7pro-uefi](https://github.com/Water500ton/pista-realme-gt7pro-uefi)

## 开源协议
除非个别文件头另有声明，本仓库中的内核补丁、设备树与驱动程序均基于 **GPL-2.0-only** 协议开源。
