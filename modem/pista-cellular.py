#!/usr/bin/env python3
"""5G/4G cellular data daemon & control CLI for Realme GT7 Pro (SM8750 IPA v5.5).

Handles:
- Automatic MPSS (`remoteproc0`) startup, radio online, and 5G NR / 4G LTE mode
- QMI DPM embedded IPA port open (EP_EMBEDDED=4, iface=1, rx=2, tx=24)
- QMI WDA raw-IP + QMAPv5 aggregation & hardware checksum offload
- QMI WDS mux_id=1 bind + IPv4 dial + `rmnet_data0` configuration
- iSCSI-safe `--default-route` (metric 100 on rmnet_data0; never touches wlan0/LAN)
- `--status` JSON snapshot and `--write-config` for the companion GTK3 GUI
"""

import argparse
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time

AF_QIPCRTR = 42
EP_EMBEDDED = 4
IPA_EP_IFACE = 1
IPA_AP_MODEM_TX_EP = 2   # DPM hw-data-rx-id (AP -> IPA -> Modem)
IPA_AP_MODEM_RX_EP = 24  # DPM hw-data-tx-id (Modem -> IPA -> AP)
RPROC_STATE = "/sys/class/remoteproc/remoteproc0/state"
RPROC_RECOVERY = "/sys/class/remoteproc/remoteproc0/recovery"
DEFAULT_CONFIG_PATH = "/etc/pista-cellular.conf"
DEFAULT_STATUS_PATH = "/run/pista-cellular.status"

PLMN_MAP = {
    ("460", "0"): "中国移动 (CMCC)",
    ("460", "00"): "中国移动 (CMCC)",
    ("460", "2"): "中国移动 (CMCC)",
    ("460", "02"): "中国移动 (CMCC)",
    ("460", "4"): "中国移动 (CMCC)",
    ("460", "04"): "中国移动 (CMCC)",
    ("460", "7"): "中国移动 (CMCC)",
    ("460", "07"): "中国移动 (CMCC)",
    ("460", "1"): "中国联通 (China Unicom)",
    ("460", "01"): "中国联通 (China Unicom)",
    ("460", "6"): "中国联通 (China Unicom)",
    ("460", "06"): "中国联通 (China Unicom)",
    ("460", "9"): "中国联通 (China Unicom)",
    ("460", "09"): "中国联通 (China Unicom)",
    ("460", "3"): "中国电信 (China Telecom)",
    ("460", "03"): "中国电信 (China Telecom)",
    ("460", "5"): "中国电信 (China Telecom)",
    ("460", "05"): "中国电信 (China Telecom)",
    ("460", "11"): "中国电信 (China Telecom)",
    ("460", "15"): "中国广电 (CBN)",
}


def tlv(tag: int, val: bytes) -> bytes:
    return struct.pack("<BH", tag, len(val)) + val


def parse_tlvs(data: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    i = 0
    while i + 3 <= len(data):
        t, length = struct.unpack_from("<BH", data, i)
        out[t] = data[i + 3 : i + 3 + length]
        i += 3 + length
    return out


def lookup_qrtr_port(service_id: int, node_id: int = 0) -> int:
    out = subprocess.check_output(["qrtr-lookup"], text=True)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].isdigit():
            if int(parts[0]) == service_id and int(parts[3]) == node_id:
                return int(parts[4])
    raise RuntimeError(f"QRTR service {service_id} on node {node_id} not found")


def qmi_call(sock: socket.socket, port: int, tx: int, msg_id: int, payload: bytes = b""):
    hdr = struct.pack("<BHHH", 0x00, tx, msg_id, len(payload))
    sock.sendto(hdr + payload, (0, port))
    while True:
        pkt, _ = sock.recvfrom(4096)
        flags, rtx, rmsg, rlen = struct.unpack_from("<BHHH", pkt, 0)
        if flags == 0x02 and rtx == tx and rmsg == msg_id:
            tlvs = parse_tlvs(pkt[7 : 7 + rlen])
            res, err = struct.unpack("<HH", tlvs[0x02])
            return res, err, tlvs


def write_status(path: str, data: dict) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        pass


def read_config(path: str) -> dict:
    cfg = {
        "apn": "",
        "profile": 1,
        "mux_id": 1,
        "iface": "rmnet_data0",
        "enable_5g": True,
        "default_route": True,
    }
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    return cfg


def read_net_stats(iface: str) -> tuple[int, int]:
    base = f"/sys/class/net/{iface}/statistics"
    try:
        rx = int(open(f"{base}/rx_bytes").read().strip())
        tx = int(open(f"{base}/tx_bytes").read().strip())
        return rx, tx
    except OSError:
        return 0, 0


def run_cmd(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def get_live_status(iface: str = "rmnet_data0") -> dict:
    st = {
        "service_active": False,
        "state": "disconnected",
        "modem_state": "offline",
        "operating_mode": "offline",
        "registration": "unregistered",
        "operator": "--",
        "rat": "--",
        "band": "--",
        "bandwidth": "--",
        "rssi": None,
        "rsrp": None,
        "sinr": None,
        "iface": iface,
        "ipv4": "",
        "gw": "",
        "dns": [],
        "default_route_dev": "",
        "rx_bytes": 0,
        "tx_bytes": 0,
        "wlan0_up": False,
        "wlan0_ip": "",
        "error": "",
    }

    # Check systemd service state & saved connection metadata
    svc = run_cmd(["systemctl", "is-active", "pista-cellular.service"], timeout=1.5).strip()
    st["service_active"] = svc in ("active", "activating")
    if os.path.exists(DEFAULT_STATUS_PATH):
        try:
            saved = json.load(open(DEFAULT_STATUS_PATH, encoding="utf-8"))
            for k in ("state", "ipv4", "gw", "dns", "error"):
                if k in saved:
                    st[k] = saved[k]
        except Exception:
            pass
    if not st["service_active"] and not os.path.exists(f"/sys/class/net/{iface}"):
        st["state"] = "disconnected"
        st["ipv4"] = ""
        st["gw"] = ""
        st["dns"] = []

    # Modem remoteproc state
    try:
        st["modem_state"] = open(RPROC_STATE).read().strip()
    except OSError:
        pass

    # Network interfaces & routes
    if os.path.exists(f"/sys/class/net/{iface}"):
        st["rx_bytes"], st["tx_bytes"] = read_net_stats(iface)
        ip_out = run_cmd(["ip", "-4", "-br", "addr", "show", iface], timeout=1.5)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", ip_out)
        if m:
            st["ipv4"] = m.group(1)
            st["state"] = "connected"

    wlan_out = run_cmd(["ip", "-4", "-br", "addr", "show", "wlan0"], timeout=1.5)
    st["wlan0_up"] = "UP" in wlan_out
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", wlan_out)
    if m:
        st["wlan0_ip"] = m.group(1)

    rt = run_cmd(["ip", "route", "show", "default"], timeout=1.5)
    for line in rt.splitlines():
        m = re.search(r"\bdev\s+(\S+)", line)
        if m:
            st["default_route_dev"] = m.group(1)
            break

    # QMI radio / signal queries when MPSS is running
    if st["modem_state"] == "running":
        dms = run_cmd(["qmicli", "-d", "qrtr://0", "--dms-get-operating-mode"], timeout=2.0)
        m = re.search(r"Mode:\s*'([^']+)'", dms)
        if m:
            st["operating_mode"] = m.group(1)

        nas = run_cmd(["qmicli", "-d", "qrtr://0", "--nas-get-serving-system"], timeout=2.0)
        m = re.search(r"Registration state:\s*'([^']+)'", nas)
        if m:
            st["registration"] = m.group(1)
        mcc_m = re.search(r"MCC:\s*'(\d+)'", nas)
        mnc_m = re.search(r"MNC:\s*'(\d+)'", nas)
        if mcc_m and mnc_m:
            mcc, mnc = mcc_m.group(1), mnc_m.group(1)
            st["operator"] = PLMN_MAP.get((mcc, mnc), f"PLMN {mcc}-{mnc}")

        band = run_cmd(["qmicli", "-d", "qrtr://0", "--nas-get-rf-band-info"], timeout=2.0)
        m_rat = re.search(r"Radio Interface:\s*'([^']+)'", band)
        m_bnd = re.search(r"Active Band Class:\s*'([^']+)'", band)
        m_bw = re.search(r"Bandwidth:\s*'([^']+)'", band)
        if m_rat:
            rat = m_rat.group(1).lower()
            st["rat"] = "5G NR" if rat == "5gnr" else rat.upper()
        if m_bnd:
            b = m_bnd.group(1)
            b = b.replace("nr5g-", "n").replace("eutran-", "B")
            st["band"] = b
        if m_bw:
            bw = m_bw.group(1).replace("5gnr-", "")
            st["bandwidth"] = f"{bw} MHz" if bw.isdigit() else bw

        sig = run_cmd(["qmicli", "-d", "qrtr://0", "--nas-get-signal-info"], timeout=2.0)
        m_rsrp = re.search(r"RSRP:\s*'(-?\d+)\s*dBm'", sig)
        m_rssi = re.search(r"RSSI:\s*'(-?\d+)\s*dBm'", sig)
        m_snr = re.search(r"SNR:\s*'(-?[\d.]+)\s*dB'", sig)
        if m_rsrp:
            st["rsrp"] = int(m_rsrp.group(1))
        if m_rssi:
            st["rssi"] = int(m_rssi.group(1))
        if m_snr:
            st["sinr"] = float(m_snr.group(1))

    return st


def ensure_modem_registered(enable_5g: bool, status_path: str) -> None:
    if os.path.exists(RPROC_STATE):
        state = open(RPROC_STATE).read().strip()
        if state != "running":
            write_status(status_path, {"state": "starting_modem", "error": ""})
            print("Starting MPSS (remoteproc0)...", flush=True)
            try:
                open(RPROC_RECOVERY, "w").write("disabled")
            except OSError:
                pass
            open(RPROC_STATE, "w").write("start")
            for _ in range(24):
                if os.path.exists("/sys/class/net/rmnet_ipa0"):
                    break
                time.sleep(0.5)
            time.sleep(2.0)

    mode = run_cmd(["qmicli", "-d", "qrtr://0", "--dms-get-operating-mode"], timeout=3.0)
    if "'online'" not in mode:
        write_status(status_path, {"state": "enabling_radio", "error": ""})
        print("Bringing modem radio online...", flush=True)
        subprocess.run(["qmicli", "-d", "qrtr://0", "--dms-set-operating-mode=online"], check=True)
        time.sleep(1.5)

    pref = "5gnr|lte|umts|gsm" if enable_5g else "lte|umts|gsm"
    subprocess.run(
        ["qmicli", "-d", "qrtr://0", f"--nas-set-system-selection-preference={pref}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    write_status(status_path, {"state": "registering", "error": ""})
    print("Waiting for cellular network registration...", flush=True)
    for _ in range(30):
        out = run_cmd(["qmicli", "-d", "qrtr://0", "--nas-get-serving-system"], timeout=3.0)
        if "Registration state: 'registered'" in out:
            return
        time.sleep(1.0)
    raise RuntimeError("Timed out waiting for cellular registration")


def main() -> int:
    ap = argparse.ArgumentParser(description="Realme GT7 Pro SM8750 5G/4G cellular data helper")
    ap.add_argument("--config", default="", help="Load JSON config file (e.g. /etc/pista-cellular.conf)")
    ap.add_argument("--write-config", default="", help="Write JSON config string to /etc/pista-cellular.conf and exit")
    ap.add_argument("--status", action="store_true", help="Print live JSON status and exit")
    ap.add_argument("--ping-test", default="", help="Run 3-packet ping test over rmnet_data0 to host and exit")
    ap.add_argument("--status-file", default=DEFAULT_STATUS_PATH, help="Runtime status JSON file path")
    ap.add_argument("--apn", default=None, help="3GPP APN name (e.g. 3GNET, cmnet, ctnet); empty uses --profile")
    ap.add_argument("--profile", type=int, default=None, help="3GPP profile index when --apn is empty (default: 1)")
    ap.add_argument("--mux-id", type=int, default=None, help="RMNET QMAP mux_id (default: 1)")
    ap.add_argument("--iface", default=None, help="Virtual rmnet netdev name (default: rmnet_data0)")
    ap.add_argument("--no-5g", action="store_true", help="Use 4G LTE/UMTS/GSM instead of 5G NR priority")
    ap.add_argument(
        "--default-route", "--set-default-route", dest="default_route", action="store_true",
        help="Route internet via cellular (metric 100) while keeping wlan0/LAN iSCSI routes untouched",
    )
    args = ap.parse_args()

    if args.status:
        print(json.dumps(get_live_status(args.iface or "rmnet_data0"), ensure_ascii=False))
        return 0

    if args.write_config:
        if os.geteuid() != 0:
            print("Root required for --write-config", file=sys.stderr)
            return 1
        data = json.loads(args.write_config)
        cfg = read_config(DEFAULT_CONFIG_PATH)
        for k in ("apn", "profile", "mux_id", "iface", "enable_5g", "default_route"):
            if k in data:
                cfg[k] = data[k]
        with open(DEFAULT_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.chmod(DEFAULT_CONFIG_PATH, 0o644)
        return 0

    if args.ping_test:
        iface = args.iface or "rmnet_data0"
        host = args.ping_test
        if not os.path.exists(f"/sys/class/net/{iface}"):
            print("蜂窝接口未连接", file=sys.stderr)
            return 1
        try:
            ip = socket.gethostbyname(host)
        except Exception as e:
            print(f"DNS 解析失败: {e}", file=sys.stderr)
            return 1
        ip_out = run_cmd(["ip", "-4", "-br", "addr", "show", iface], timeout=1.5)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", ip_out)
        if not m:
            print("未获取到蜂窝 IPv4", file=sys.stderr)
            return 1
        src_ip = m.group(1)
        added_route = False
        rt_check = run_cmd(["ip", "route", "get", ip], timeout=1.5)
        if f"dev {iface}" not in rt_check and os.geteuid() == 0:
            subprocess.run(["ip", "route", "replace", f"{ip}/32", "dev", iface, "src", src_ip], check=False)
            added_route = True
        try:
            r = subprocess.run(["ping", "-I", iface, "-c", "3", "-W", "3", ip], capture_output=True, text=True)
            print(r.stdout.strip() or r.stderr.strip())
            return r.returncode
        finally:
            if added_route:
                subprocess.run(["ip", "route", "del", f"{ip}/32", "dev", iface], stderr=subprocess.DEVNULL)

    if os.geteuid() != 0:
        print("Please run as root (sudo pista-cellular).", file=sys.stderr)
        return 1

    cfg = read_config(args.config)
    apn = args.apn if args.apn is not None else cfg["apn"]
    profile = args.profile if args.profile is not None else int(cfg["profile"])
    mux_id = args.mux_id if args.mux_id is not None else int(cfg["mux_id"])
    iface = args.iface if args.iface is not None else cfg["iface"]
    enable_5g = (not args.no_5g) if args.no_5g else bool(cfg["enable_5g"])
    default_route = True if args.default_route else (bool(cfg["default_route"]) if args.config else False)

    try:
        ensure_modem_registered(enable_5g=enable_5g, status_path=args.status_file)
        write_status(args.status_file, {"state": "dialing", "error": ""})

        dpm_port = lookup_qrtr_port(47)
        wds_port = lookup_qrtr_port(1)

        subprocess.run(["modprobe", "rmnet"], check=False)
        subprocess.run(["ip", "link", "set", "rmnet_ipa0", "up"], check=True)

        # 1. Open embedded IPA hardware data port and keep DPM client alive
        dpm_sock = socket.socket(AF_QIPCRTR, socket.SOCK_DGRAM, 0)
        dpm_sock.settimeout(10.0)
        res, err, _ = qmi_call(
            dpm_sock, dpm_port, 1, 0x0020,
            tlv(0x11, struct.pack("<BIIII", 1, EP_EMBEDDED, IPA_EP_IFACE, IPA_AP_MODEM_TX_EP, IPA_AP_MODEM_RX_EP)),
        )
        if res != 0:
            raise RuntimeError(f"QMI DPM Open Port failed: err={err}")

        # 2. Configure QMAPv5 raw-IP aggregation on embedded IPA endpoint
        subprocess.run(
            [
                "qmicli", "-d", "qrtr://0",
                f"--wda-set-data-format=link-layer-protocol=raw-ip,ul-protocol=qmapv5,dl-protocol=qmapv5,"
                f"dl-max-datagrams=32,dl-datagram-max-size=16384,ep-type=embedded,ep-iface-number={IPA_EP_IFACE}",
            ],
            check=True, stdout=subprocess.DEVNULL,
        )

        # 3. Bind WDS mux port and start IPv4 packet data call
        wds_sock = socket.socket(AF_QIPCRTR, socket.SOCK_DGRAM, 0)
        wds_sock.settimeout(15.0)
        res, err, _ = qmi_call(
            wds_sock, wds_port, 1, 0x00A2,
            tlv(0x10, struct.pack("<II", EP_EMBEDDED, IPA_EP_IFACE)) + tlv(0x11, struct.pack("<B", mux_id)),
        )
        if res != 0:
            raise RuntimeError(f"QMI WDS Bind Mux failed: err={err}")

        qmi_call(wds_sock, wds_port, 2, 0x004D, tlv(0x01, struct.pack("<B", 4)))

        start_tlvs = tlv(0x19, struct.pack("<B", 4))
        if apn:
            start_tlvs = tlv(0x14, apn.encode("ascii")) + start_tlvs
        else:
            start_tlvs = tlv(0x31, struct.pack("<B", profile)) + start_tlvs

        res, err, tlvs = qmi_call(wds_sock, wds_port, 3, 0x0020, start_tlvs)
        if res != 0:
            raise RuntimeError(f"QMI WDS Start Network failed: err={err}")
        pkt_handle = struct.unpack("<I", tlvs[0x01])[0]

        res, err, tlvs = qmi_call(wds_sock, wds_port, 4, 0x002D, tlv(0x10, struct.pack("<I", 0x00001FD0)))
        if res != 0:
            raise RuntimeError(f"QMI WDS Get Current Settings failed: err={err}")

        ip_str = socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x1E])[0]))
        gw_str = socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x20])[0])) if 0x20 in tlvs else ""
        dns_list = []
        if 0x15 in tlvs:
            dns_list.append(socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x15])[0])))
        if 0x16 in tlvs:
            dns_list.append(socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x16])[0])))

        # 4. Create QMAPv5 rmnet interface with hardware checksum offload
        subprocess.run(["ip", "link", "del", iface], stderr=subprocess.DEVNULL)
        subprocess.run(
            [
                "ip", "link", "add", "link", "rmnet_ipa0", "name", iface,
                "type", "rmnet", "mux_id", str(mux_id),
                "ingress-deaggregation", "on",
                "ingress-mapv5-checksum", "on",
                "egress-mapv5-checksum", "on",
            ],
            check=True,
        )
        subprocess.run(["ip", "link", "set", iface, "up"], check=True)
        subprocess.run(["ip", "addr", "add", f"{ip_str}/32", "dev", iface], check=True)
        if default_route:
            subprocess.run(
                ["ip", "route", "replace", "default", "dev", iface, "src", ip_str, "metric", "100"],
                check=True,
            )

        write_status(args.status_file, {
            "state": "connected",
            "ipv4": ip_str,
            "gw": gw_str,
            "dns": dns_list,
            "default_route": default_route,
            "error": "",
        })
        print(f"Connected on {iface}: IPv4={ip_str} GW={gw_str} DNS={','.join(dns_list)}", flush=True)
    except Exception as e:
        write_status(args.status_file, {"state": "error", "error": str(e)})
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    stop = False

    def _sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    while not stop:
        time.sleep(0.5)

    subprocess.run(["ip", "route", "del", "default", "dev", iface, "metric", "100"], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", iface], stderr=subprocess.DEVNULL)
    try:
        qmi_call(wds_sock, wds_port, 5, 0x0021, tlv(0x01, struct.pack("<I", pkt_handle)))
    except Exception:
        pass
    write_status(args.status_file, {"state": "disconnected", "ipv4": "", "gw": "", "dns": [], "error": ""})
    print("Disconnected cleanly.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
