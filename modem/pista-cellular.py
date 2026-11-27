#!/usr/bin/env python3
"""Minimal QRTR/QMI cellular data bringup helper for Realme GT7 Pro (SM8750 IPA v5.5).

Opens the embedded IPA hardware data port via QMI DPM (EP_EMBEDDED=4, iface=1,
rx=2, tx=24), configures QMAPv5 raw-IP aggregation via WDA, binds mux_id=1 on
`rmnet_data0` (with QMAPv5 hardware checksum offload), dials the requested 3GPP
APN via WDS, and configures IPv4 on `rmnet_data0`.

By default this script does NOT modify the system default route (so Wi-Fi iSCSI
root filesystems remain untouched). Pass `--set-default-route` only when running
from local storage or when intentionally routing all traffic over cellular.
"""

import argparse
import os
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


def main() -> int:
    ap = argparse.ArgumentParser(description="SM8750 IPA v5.5 QMAPv5 cellular dialer")
    ap.add_argument("--apn", default="", help="3GPP APN name (e.g. internet, 3GNET); omit to use --profile")
    ap.add_argument("--profile", type=int, default=1, help="3GPP profile index when --apn is empty (default: 1)")
    ap.add_argument("--mux-id", type=int, default=1, help="RMNET QMAP mux_id (default: 1)")
    ap.add_argument("--iface", default="rmnet_data0", help="Virtual rmnet netdev name (default: rmnet_data0)")
    ap.add_argument("--set-default-route", action="store_true", help="Add default route via rmnet (DANGEROUS on Wi-Fi iSCSI root)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("Run as root.", file=sys.stderr)
        return 1

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
        check=True,
    )

    # 3. Bind WDS mux port and start IPv4 packet data call
    wds_sock = socket.socket(AF_QIPCRTR, socket.SOCK_DGRAM, 0)
    wds_sock.settimeout(15.0)
    res, err, _ = qmi_call(
        wds_sock, wds_port, 1, 0x00A2,
        tlv(0x10, struct.pack("<II", EP_EMBEDDED, IPA_EP_IFACE)) + tlv(0x11, struct.pack("<B", args.mux_id)),
    )
    if res != 0:
        raise RuntimeError(f"QMI WDS Bind Mux failed: err={err}")

    qmi_call(wds_sock, wds_port, 2, 0x004D, tlv(0x01, struct.pack("<B", 4)))

    start_tlvs = tlv(0x19, struct.pack("<B", 4))
    if args.apn:
        start_tlvs = tlv(0x14, args.apn.encode("ascii")) + start_tlvs
    else:
        start_tlvs = tlv(0x31, struct.pack("<B", args.profile)) + start_tlvs

    res, err, tlvs = qmi_call(wds_sock, wds_port, 3, 0x0020, start_tlvs)
    if res != 0:
        raise RuntimeError(f"QMI WDS Start Network failed: err={err}")
    pkt_handle = struct.unpack("<I", tlvs[0x01])[0]

    res, err, tlvs = qmi_call(wds_sock, wds_port, 4, 0x002D, tlv(0x10, struct.pack("<I", 0x00001FD0)))
    if res != 0:
        raise RuntimeError(f"QMI WDS Get Current Settings failed: err={err}")

    ip_str = socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x1E])[0]))
    gw_str = socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x20])[0])) if 0x20 in tlvs else ""
    dns1 = socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x15])[0])) if 0x15 in tlvs else ""
    dns2 = socket.inet_ntoa(struct.pack(">I", struct.unpack("<I", tlvs[0x16])[0])) if 0x16 in tlvs else ""

    # 4. Create QMAPv5 rmnet interface with hardware checksum offload
    subprocess.run(["ip", "link", "del", args.iface], stderr=subprocess.DEVNULL)
    subprocess.run(
        [
            "ip", "link", "add", "link", "rmnet_ipa0", "name", args.iface,
            "type", "rmnet", "mux_id", str(args.mux_id),
            "ingress-deaggregation", "on",
            "ingress-mapv5-checksum", "on",
            "egress-mapv5-checksum", "on",
        ],
        check=True,
    )
    subprocess.run(["ip", "link", "set", args.iface, "up"], check=True)
    subprocess.run(["ip", "addr", "add", f"{ip_str}/32", "dev", args.iface], check=True)
    if args.set_default_route:
        subprocess.run(["ip", "route", "replace", "default", "dev", args.iface, "src", ip_str, "metric", "500"], check=True)

    print(f"Connected on {args.iface}: IPv4={ip_str} GW={gw_str} DNS={dns1},{dns2} (handle=0x{pkt_handle:08x})", flush=True)
    print("Press Ctrl+C to disconnect cleanly.", flush=True)

    stop = False

    def _sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    while not stop:
        time.sleep(0.5)

    if args.set_default_route:
        subprocess.run(["ip", "route", "del", "default", "dev", args.iface], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", args.iface], stderr=subprocess.DEVNULL)
    qmi_call(wds_sock, wds_port, 5, 0x0021, tlv(0x01, struct.pack("<I", pkt_handle)))
    print("Disconnected cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
