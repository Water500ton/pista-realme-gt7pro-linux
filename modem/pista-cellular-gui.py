#!/usr/bin/env python3
"""Touch-friendly GTK3 5G/4G Cellular Network GUI for Realme GT7 Pro (SM8750 IPA v5.5)."""

import json
import os
import subprocess
import threading
import time
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

CONFIG_PATH = "/etc/pista-cellular.conf"
CTL_BIN = "/usr/local/bin/pista-cellular"
SERVICE_NAME = "pista-cellular.service"

CSS = b"""
window {
    background-color: #12151c;
    color: #e6edf7;
}
.card {
    background-color: #1b202b;
    border-radius: 16px;
    padding: 16px;
    border: 1px solid #2b3344;
}
.title-lbl {
    font-size: 20px;
    font-weight: 700;
    color: #ffffff;
}
.sub-lbl {
    font-size: 13px;
    color: #93a1b8;
}
.big-status {
    font-size: 18px;
    font-weight: 700;
}
.st-ok { color: #3ddc84; }
.st-wait { color: #f5b942; }
.st-off { color: #93a1b8; }
.st-err { color: #ff6b6b; }
.metric-val {
    font-size: 15px;
    font-weight: 600;
    color: #e6edf7;
}
.btn-connect {
    background-image: none;
    background-color: #2563eb;
    color: #ffffff;
    border-radius: 14px;
    font-size: 17px;
    font-weight: 700;
    min-height: 54px;
    border: none;
}
.btn-connect:hover { background-color: #1d4ed8; }
.btn-disconnect {
    background-image: none;
    background-color: #dc2626;
    color: #ffffff;
    border-radius: 14px;
    font-size: 17px;
    font-weight: 700;
    min-height: 54px;
    border: none;
}
.btn-disconnect:hover { background-color: #b91c1c; }
.btn-secondary {
    background-image: none;
    background-color: #262e3e;
    color: #e6edf7;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 600;
    min-height: 44px;
    border: 1px solid #384359;
}
.btn-secondary:hover { background-color: #333e54; }
.wifi-ok {
    background-color: #132a20;
    border: 1px solid #245c43;
    border-radius: 10px;
    padding: 8px 12px;
    color: #7ee2b8;
    font-size: 12px;
}
.wifi-warn {
    background-color: #301c1c;
    border: 1px solid #6b2e2e;
    border-radius: 10px;
    padding: 8px 12px;
    color: #ff9c9c;
    font-size: 12px;
}
.ping-box {
    background-color: #0e1117;
    border-radius: 10px;
    padding: 10px;
    font-family: monospace;
    font-size: 12px;
    color: #b8c7de;
}
"""


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def signal_bars(rsrp: int | None) -> str:
    if rsrp is None:
        return "▁ ▂ ▄ ▆ ▇  (--)"
    if rsrp >= -85:
        return "█████  极佳"
    if rsrp >= -95:
        return "████░  良好"
    if rsrp >= -105:
        return "███░░  一般"
    if rsrp >= -115:
        return "██░░░  较弱"
    return "█░░░░  微弱"


class CellularWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="5G 蜂窝网络 · Realme GT7 Pro")
        self.set_icon_name("smartphone")
        self.set_default_size(460, 860)
        self.set_border_width(14)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.busy = False
        self.connected = False
        self.last_rx = 0
        self.last_tx = 0
        self.last_ts = time.monotonic()

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.add(scroll)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroll.add(root)

        # Header
        hdr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        t = Gtk.Label(label="5G 蜂窝网络", xalign=0)
        t.get_style_context().add_class("title-lbl")
        s = Gtk.Label(label="Snapdragon 8 Elite · SM8750 IPA v5.5 (QMAPv5)", xalign=0)
        s.get_style_context().add_class("sub-lbl")
        hdr.pack_start(t, False, False, 0)
        hdr.pack_start(s, False, False, 0)
        root.pack_start(hdr, False, False, 0)

        # Wi-Fi iSCSI safety banner
        self.wifi_lbl = Gtk.Label(label="Wi-Fi / iSCSI 状态检测中...", xalign=0)
        self.wifi_lbl.set_line_wrap(True)
        self.wifi_lbl.get_style_context().add_class("wifi-ok")
        root.pack_start(self.wifi_lbl, False, False, 0)

        # Main status & connect card
        status_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        status_card.get_style_context().add_class("card")

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.lbl_state = Gtk.Label(label="未连接", xalign=0)
        self.lbl_state.get_style_context().add_class("big-status")
        self.lbl_state.get_style_context().add_class("st-off")
        self.lbl_rat = Gtk.Label(label="--", xalign=1)
        self.lbl_rat.get_style_context().add_class("metric-val")
        top_row.pack_start(self.lbl_state, True, True, 0)
        top_row.pack_end(self.lbl_rat, False, False, 0)
        status_card.pack_start(top_row, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.val_operator = self._add_row(grid, 0, "运营商")
        self.val_signal = self._add_row(grid, 1, "信号强度")
        self.val_ip = self._add_row(grid, 2, "蜂窝 IPv4")
        self.val_route = self._add_row(grid, 3, "当前默认出口")
        self.val_traffic = self._add_row(grid, 4, "流量 (下行 / 上行)")
        status_card.pack_start(grid, False, False, 0)

        self.btn_toggle = Gtk.Button(label="开启 5G/4G 蜂窝数据")
        self.btn_toggle.get_style_context().add_class("btn-connect")
        self.btn_toggle.connect("clicked", self.on_toggle_clicked)
        status_card.pack_start(self.btn_toggle, False, False, 4)

        root.pack_start(status_card, False, False, 0)

        # Settings card
        cfg_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cfg_card.get_style_context().add_class("card")
        cfg_title = Gtk.Label(label="连接设置", xalign=0)
        cfg_title.get_style_context().add_class("metric-val")
        cfg_card.pack_start(cfg_title, False, False, 0)

        # Default route switch
        r1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        r1_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r1_l1 = Gtk.Label(label="全局上网优先走蜂窝数据", xalign=0)
        r1_l2 = Gtk.Label(label="自动保留 wlan0 局域网 / iSCSI 根盘不断线", xalign=0)
        r1_l2.get_style_context().add_class("sub-lbl")
        r1_box.pack_start(r1_l1, False, False, 0)
        r1_box.pack_start(r1_l2, False, False, 0)
        self.sw_default = Gtk.Switch()
        self.sw_default.set_valign(Gtk.Align.CENTER)
        r1.pack_start(r1_box, True, True, 0)
        r1.pack_end(self.sw_default, False, False, 0)
        cfg_card.pack_start(r1, False, False, 0)

        # 5G priority switch
        r2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        r2_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r2_l1 = Gtk.Label(label="5G NR 优先 (5G SA / 4G LTE)", xalign=0)
        r2_l2 = Gtk.Label(label="关闭则仅使用 4G LTE / 3G / 2G", xalign=0)
        r2_l2.get_style_context().add_class("sub-lbl")
        r2_box.pack_start(r2_l1, False, False, 0)
        r2_box.pack_start(r2_l2, False, False, 0)
        self.sw_5g = Gtk.Switch()
        self.sw_5g.set_valign(Gtk.Align.CENTER)
        r2.pack_start(r2_box, True, True, 0)
        r2.pack_end(self.sw_5g, False, False, 0)
        cfg_card.pack_start(r2, False, False, 0)

        # APN preset selector
        r3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        r3.pack_start(Gtk.Label(label="APN 接入点", xalign=0), False, False, 0)
        self.combo_apn = Gtk.ComboBoxText.new_with_entry()
        for item in ("自动 (SIM Profile 1)", "3GNET", "cmnet", "ctnet", "cbnet"):
            self.combo_apn.append_text(item)
        self.combo_apn.set_active(0)
        r3.pack_end(self.combo_apn, True, True, 0)
        cfg_card.pack_start(r3, False, False, 0)

        root.pack_start(cfg_card, False, False, 0)

        # Diagnostics card
        diag_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        diag_card.get_style_context().add_class("card")
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_ping = Gtk.Button(label="测延迟 (ping baidu.com)")
        self.btn_ping.get_style_context().add_class("btn-secondary")
        self.btn_ping.connect("clicked", self.on_ping_clicked)
        self.btn_refresh = Gtk.Button(label="立即刷新")
        self.btn_refresh.get_style_context().add_class("btn-secondary")
        self.btn_refresh.connect("clicked", lambda _b: self.poll_async())
        btn_row.pack_start(self.btn_ping, True, True, 0)
        btn_row.pack_start(self.btn_refresh, False, False, 0)
        diag_card.pack_start(btn_row, False, False, 0)

        self.lbl_log = Gtk.Label(label="就绪。点击上方按钮即可一键开启 5G/4G 蜂窝网络。", xalign=0)
        self.lbl_log.set_line_wrap(True)
        self.lbl_log.set_selectable(True)
        self.lbl_log.get_style_context().add_class("ping-box")
        diag_card.pack_start(self.lbl_log, False, False, 0)
        root.pack_start(diag_card, False, False, 0)

        self._load_initial_config()
        self.poll_async()
        GLib.timeout_add(2500, self._on_timer)

    def _add_row(self, grid: Gtk.Grid, row: int, label: str) -> Gtk.Label:
        k = Gtk.Label(label=label, xalign=0)
        k.get_style_context().add_class("sub-lbl")
        v = Gtk.Label(label="--", xalign=1)
        v.get_style_context().add_class("metric-val")
        v.set_hexpand(True)
        grid.attach(k, 0, row, 1, 1)
        grid.attach(v, 1, row, 1, 1)
        return v

    def _load_initial_config(self) -> None:
        cfg = {"apn": "", "enable_5g": True, "default_route": True}
        if os.path.exists(CONFIG_PATH):
            try:
                cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
            except Exception:
                pass
        self.sw_default.set_active(bool(cfg.get("default_route", True)))
        self.sw_5g.set_active(bool(cfg.get("enable_5g", True)))
        apn = cfg.get("apn", "")
        if apn:
            self.combo_apn.get_child().set_text(apn)
        else:
            self.combo_apn.set_active(0)

    def _on_timer(self) -> bool:
        if not self.busy:
            self.poll_async()
        return True

    def poll_async(self) -> None:
        def worker():
            try:
                r = subprocess.run([CTL_BIN, "--status"], capture_output=True, text=True, timeout=6)
                if r.returncode == 0 and r.stdout.strip():
                    data = json.loads(r.stdout)
                    GLib.idle_add(self._apply_status, data)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, st: dict) -> None:
        now = time.monotonic()
        dt = max(now - self.last_ts, 0.2)
        rx = int(st.get("rx_bytes", 0))
        tx = int(st.get("tx_bytes", 0))
        rx_rate = max(0, rx - self.last_rx) / dt if self.last_rx else 0
        tx_rate = max(0, tx - self.last_tx) / dt if self.last_tx else 0
        self.last_rx, self.last_tx, self.last_ts = rx, tx, now

        state = st.get("state", "disconnected")
        svc_active = bool(st.get("service_active", False))
        self.connected = (state == "connected") or svc_active

        # Status label + color
        ctx = self.lbl_state.get_style_context()
        for c in ("st-ok", "st-wait", "st-off", "st-err"):
            ctx.remove_class(c)

        state_map = {
            "connected": ("已连接", "st-ok"),
            "starting_modem": ("正在启动基带...", "st-wait"),
            "enabling_radio": ("正在开启射频...", "st-wait"),
            "registering": ("正在搜网注册...", "st-wait"),
            "dialing": ("正在拨号获取 IP...", "st-wait"),
            "error": ("连接失败", "st-err"),
            "disconnected": ("未连接", "st-off"),
        }
        txt, cls = state_map.get(state, (state, "st-off"))
        self.lbl_state.set_text(txt)
        ctx.add_class(cls)

        if st.get("error"):
            self.lbl_log.set_text(f"错误: {st['error']}")

        rat = st.get("rat", "--")
        band = st.get("band", "--")
        bw = st.get("bandwidth", "--")
        if rat != "--" and band != "--":
            self.lbl_rat.set_text(f"{rat} · {band} ({bw})" if bw != "--" else f"{rat} · {band}")
        else:
            self.lbl_rat.set_text(st.get("modem_state", "offline"))

        self.val_operator.set_text(st.get("operator", "--"))
        rsrp = st.get("rsrp")
        rssi = st.get("rssi")
        if rsrp is not None:
            sig_txt = f"{signal_bars(rsrp)}  ({rsrp} dBm)"
        elif rssi is not None:
            sig_txt = f"RSSI {rssi} dBm"
        else:
            sig_txt = "--"
        self.val_signal.set_text(sig_txt)

        ip_str = st.get("ipv4", "")
        self.val_ip.set_text(ip_str if ip_str else "--")
        rt_dev = st.get("default_route_dev", "--")
        rt_name = f"{rt_dev} (蜂窝)" if rt_dev.startswith("rmnet") else f"{rt_dev} (Wi-Fi)" if rt_dev == "wlan0" else rt_dev
        self.val_route.set_text(rt_name)
        if rx or tx:
            self.val_traffic.set_text(
                f"↓ {fmt_bytes(rx)} ({fmt_bytes(int(rx_rate))}/s)  ↑ {fmt_bytes(tx)}"
            )
        else:
            self.val_traffic.set_text("--")

        # Wi-Fi iSCSI safety bar
        wctx = self.wifi_lbl.get_style_context()
        if st.get("wlan0_up"):
            wctx.remove_class("wifi-warn")
            wctx.add_class("wifi-ok")
            self.wifi_lbl.set_text(f"● Wi-Fi 7 (wlan0: {st.get('wlan0_ip', '')}) 正常在线 · iSCSI 根盘保护中")
        else:
            wctx.remove_class("wifi-ok")
            wctx.add_class("wifi-warn")
            self.wifi_lbl.set_text("▲ Wi-Fi (wlan0) 未连接")

        if not self.busy:
            bctx = self.btn_toggle.get_style_context()
            if self.connected:
                bctx.remove_class("btn-connect")
                bctx.add_class("btn-disconnect")
                self.btn_toggle.set_label("断开蜂窝数据")
            else:
                bctx.remove_class("btn-disconnect")
                bctx.add_class("btn-connect")
                self.btn_toggle.set_label("开启 5G/4G 蜂窝数据")
            self.btn_toggle.set_sensitive(True)

    def on_toggle_clicked(self, _btn) -> None:
        if self.busy:
            return
        self.busy = True
        self.btn_toggle.set_sensitive(False)

        if self.connected:
            self.btn_toggle.set_label("正在断开...")
            self.lbl_log.set_text("正在停止蜂窝数据并恢复 Wi-Fi 默认路由...")

            def stop_worker():
                r = subprocess.run(["sudo", "-n", "systemctl", "stop", SERVICE_NAME], capture_output=True, text=True)
                msg = "已断开蜂窝数据，默认出口已切回 Wi-Fi。" if r.returncode == 0 else f"断开失败: {r.stderr.strip()}"
                GLib.idle_add(self._finish_action, msg)

            threading.Thread(target=stop_worker, daemon=True).start()
        else:
            apn_raw = self.combo_apn.get_child().get_text().strip()
            apn = "" if (not apn_raw or apn_raw.startswith("自动")) else apn_raw
            cfg = {
                "apn": apn,
                "profile": 1,
                "mux_id": 1,
                "iface": "rmnet_data0",
                "enable_5g": self.sw_5g.get_active(),
                "default_route": self.sw_default.get_active(),
            }
            self.btn_toggle.set_label("正在连接 5G/4G...")
            self.lbl_log.set_text("正在保存配置并启动 SM8750 基带与 IPA v5.5 拨号（首次启动约 5~10 秒）...")

            def start_worker():
                r1 = subprocess.run(
                    ["sudo", "-n", CTL_BIN, "--write-config", json.dumps(cfg)],
                    capture_output=True, text=True,
                )
                if r1.returncode != 0:
                    GLib.idle_add(self._finish_action, f"写入配置失败: {r1.stderr.strip()}")
                    return
                r2 = subprocess.run(["sudo", "-n", "systemctl", "restart", SERVICE_NAME], capture_output=True, text=True)
                if r2.returncode != 0:
                    GLib.idle_add(self._finish_action, f"启动服务失败: {r2.stderr.strip()}")
                    return
                # Poll briefly until connected or error
                for _ in range(25):
                    time.sleep(0.8)
                    sr = subprocess.run([CTL_BIN, "--status"], capture_output=True, text=True)
                    if sr.returncode == 0 and sr.stdout.strip():
                        st = json.loads(sr.stdout)
                        GLib.idle_add(self._apply_status, st)
                        if st.get("state") == "connected":
                            GLib.idle_add(
                                self._finish_action,
                                f"连接成功！IPv4: {st.get('ipv4')} · {st.get('rat')} {st.get('band')}",
                            )
                            return
                        if st.get("state") == "error":
                            GLib.idle_add(self._finish_action, f"连接失败: {st.get('error')}")
                            return
                GLib.idle_add(self._finish_action, "仍在拨号中，请稍候自动刷新...")

            threading.Thread(target=start_worker, daemon=True).start()

    def _finish_action(self, msg: str) -> None:
        self.busy = False
        self.lbl_log.set_text(msg)
        self.poll_async()

    def on_ping_clicked(self, _btn) -> None:
        self.btn_ping.set_sensitive(False)
        self.lbl_log.set_text("正在通过 rmnet_data0 测试 ping baidu.com ...")

        def ping_worker():
            r = subprocess.run(
                ["sudo", "-n", CTL_BIN, "--ping-test", "baidu.com"],
                capture_output=True, text=True, timeout=15,
            )
            out = (r.stdout.strip() or r.stderr.strip() or "无输出")
            GLib.idle_add(self._finish_ping, out)

        threading.Thread(target=ping_worker, daemon=True).start()

    def _finish_ping(self, out: str) -> None:
        self.btn_ping.set_sensitive(True)
        self.lbl_log.set_text(out)


def main() -> None:
    win = CellularWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
