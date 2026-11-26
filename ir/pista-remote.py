#!/usr/bin/env python3
"""
Pista IR Remote — Touch-friendly Linux IR Remote GUI for Realme GT7 Pro (/dev/lirc0).

Features:
- Built-in verified Xiaomi TV / Mi Box (小米电视 / 小米盒子) 38 kHz 4-level PPM IR preset
- Import and transmit any Flipper Zero (.ir) file (raw + NEC/NECext/Samsung32)
- Built-in rear flashlight (/sys/class/leds/white:flash) quick toggle
- Non-blocking LIRC transmission thread + live status feedback
"""

import fcntl
import glob
import os
import struct
import threading
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

LIRC_DEV = "/dev/lirc0"
FLASH_LED_PATH = "/sys/class/leds/white:flash/brightness"
USER_IR_DIR = os.path.expanduser("~/.local/share/pista-remote")

# Linux LIRC ioctls (include/uapi/linux/lirc.h)
LIRC_SET_SEND_CARRIER = 0x40046913
LIRC_SET_SEND_DUTY_CYCLE = 0x40046915


def encode_xiaomi_tv(code20: int, repeats: int = 3) -> list[int]:
    """Encode a 20-bit Xiaomi TV / Mi Box IR frame into microsecond pulse/space timings."""
    space_table = (588, 882, 1176, 1470)
    frame = [987, 605]
    for shift in range(18, -1, -2):
        sym = (code20 >> shift) & 0x3
        frame.append(587)
        frame.append(space_table[sym])
    frame.append(587)

    pulses: list[int] = []
    for r in range(repeats):
        if r > 0:
            pulses.append(12500)
        pulses.extend(frame)
    return pulses


def encode_nec(address: bytes, command: bytes, extended: bool = False) -> list[int]:
    """Encode NEC / NECext 32-bit IR frame."""
    if extended:
        bits32 = (
            address[0]
            | (address[1] << 8)
            | (command[0] << 16)
            | (command[1] << 24)
        )
    else:
        bits32 = (
            address[0]
            | ((~address[0] & 0xFF) << 8)
            | (command[0] << 16)
            | ((~command[0] & 0xFF) << 24)
        )
    pulses = [9000, 4500]
    for i in range(32):
        pulses.append(562)
        pulses.append(1687 if ((bits32 >> i) & 1) else 562)
    pulses.append(562)
    return pulses


def encode_samsung32(address: bytes, command: bytes) -> list[int]:
    """Encode Samsung32 IR frame."""
    bits32 = (
        address[0]
        | (address[0] << 8)
        | (command[0] << 16)
        | ((~command[0] & 0xFF) << 24)
    )
    pulses = [4500, 4500]
    for i in range(32):
        pulses.append(560)
        pulses.append(1690 if ((bits32 >> i) & 1) else 560)
    pulses.append(560)
    return pulses


def parse_flipper_ir(path: str) -> list[dict]:
    """Parse a Flipper Zero .ir file into a list of transmittable signals."""
    signals: list[dict] = []
    cur: dict = {}

    def flush():
        if not cur.get("name"):
            return
        sig_type = cur.get("type", "")
        if sig_type == "raw" and "data" in cur:
            raw_vals = [int(x) for x in cur["data"].split() if x.isdigit()]
            if len(raw_vals) % 2 == 0 and raw_vals:
                raw_vals = raw_vals[:-1]
            if raw_vals:
                freq = int(cur.get("frequency", 38000))
                duty = int(round(float(cur.get("duty_cycle", 0.33)) * 100))
                signals.append(
                    {
                        "name": cur["name"],
                        "freq": freq,
                        "duty": max(10, min(90, duty)),
                        "pulses": raw_vals,
                        "info": f"RAW {freq // 1000}kHz",
                    }
                )
        elif sig_type == "parsed":
            proto = cur.get("protocol", "").upper()
            addr = bytes(int(x, 16) for x in cur.get("address", "00 00 00 00").split())
            cmd = bytes(int(x, 16) for x in cur.get("command", "00 00 00 00").split())
            pulses = None
            if proto == "NEC":
                pulses = encode_nec(addr, cmd, extended=False)
            elif proto in ("NECEXT", "NEC42", "NEC42EXT"):
                pulses = encode_nec(addr, cmd, extended=True)
            elif proto == "SAMSUNG32":
                pulses = encode_samsung32(addr, cmd)
            if pulses:
                signals.append(
                    {
                        "name": cur["name"],
                        "freq": 38000,
                        "duty": 33,
                        "pulses": pulses,
                        "info": f"{proto} {addr[:2].hex()}:{cmd[:2].hex()}",
                    }
                )

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            if k == "name":
                flush()
                cur = {"name": v}
            elif k in ("type", "protocol", "address", "command", "frequency", "duty_cycle", "data"):
                cur[k] = v
    flush()
    return signals


# Verified Xiaomi TV / Mi Box IR codes (from Flipper-IRDB hardware captures)
# Format: 20-bit [addr(8)=0x86][sub(4)=0x0][cmd(4)][xor(4)] (Power uses 0x3cccf)
XIAOMI_TV_KEYS = {
    "power": ("电源", 0x3CCCF),
    "mute": ("静音", 0x86097),
    "up": ("▲", 0x8605B),
    "down": ("▼", 0x86068),
    "left": ("◀", 0x860B5),
    "right": ("▶", 0x860C2),
    "ok": ("OK", 0x860D3),
    "back": ("返回", 0x86079),
    "home": ("主页", 0x86086),
    "menu": ("菜单", 0x8604A),
    "vol_up": ("音量 +", 0x860E0),
    "vol_down": ("音量 −", 0x860F1),
}

CSS = b"""
window {
    background-color: #111318;
    color: #eef1f6;
}
.header-title {
    font-size: 20px;
    font-weight: 700;
    color: #ffffff;
}
.header-sub {
    font-size: 12px;
    color: #8f9bb3;
}
button {
    background-image: none;
    background-color: #202531;
    color: #eef1f6;
    border: 1px solid #2d3446;
    border-radius: 18px;
    font-size: 17px;
    font-weight: 600;
    padding: 12px;
    box-shadow: none;
    transition: background-color 100ms ease;
}
button:hover {
    background-color: #2a3142;
}
button:active {
    background-color: #3d7eff;
    color: #ffffff;
    border-color: #5b93ff;
}
button.power-btn {
    background-color: #c93232;
    border-color: #e04848;
    color: #ffffff;
    border-radius: 999px;
    font-size: 18px;
    font-weight: 700;
}
button.power-btn:active {
    background-color: #ff5555;
}
button.ok-btn {
    background-color: #2f6bff;
    border-color: #4f84ff;
    color: #ffffff;
    border-radius: 999px;
    font-size: 20px;
    font-weight: 700;
}
button.ok-btn:active {
    background-color: #6392ff;
}
button.dpad-btn {
    background-color: #1c222e;
    border-radius: 22px;
    font-size: 22px;
}
button.pill-btn {
    border-radius: 20px;
    font-size: 16px;
}
button.torch-on {
    background-color: #f59e0b;
    border-color: #fbbf24;
    color: #111318;
}
.dpad-frame {
    background-color: #171b24;
    border: 1px solid #252c3b;
    border-radius: 36px;
    padding: 14px;
}
.status-bar {
    background-color: #171b24;
    border: 1px solid #242b38;
    border-radius: 14px;
    padding: 10px 14px;
    font-size: 13px;
    color: #9fb0cc;
}
notebook > header {
    background-color: #161a22;
    border-bottom: 1px solid #252c3b;
}
notebook > header > tabs > tab {
    padding: 10px 16px;
    color: #9aa7bd;
    font-size: 14px;
    font-weight: 600;
}
notebook > header > tabs > tab:checked {
    color: #ffffff;
    border-bottom: 3px solid #3d7eff;
}
"""


class PistaRemoteWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="小米红外遥控器 · Pista IR")
        self.set_default_size(430, 780)
        self.set_border_width(14)

        self._tx_lock = threading.Lock()
        self._torch_on = self._read_torch_state()
        self._custom_signals: list[dict] = []

        os.makedirs(USER_IR_DIR, exist_ok=True)
        self._apply_css()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(root)

        # Top title + hardware badge
        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_title = Gtk.Label(label="小米电视红外遥控器", xalign=0)
        lbl_title.get_style_context().add_class("header-title")
        dev_ok = os.path.exists(LIRC_DEV)
        lbl_sub = Gtk.Label(
            label=f"Realme GT7 Pro · {LIRC_DEV} ({'就绪' if dev_ok else '未检测到'})",
            xalign=0,
        )
        lbl_sub.get_style_context().add_class("header-sub")
        title_col.pack_start(lbl_title, False, False, 0)
        title_col.pack_start(lbl_sub, False, False, 0)
        top_box.pack_start(title_col, True, True, 0)

        self.torch_btn = Gtk.Button(label="手电筒")
        self.torch_btn.set_size_request(86, 44)
        self.torch_btn.connect("clicked", self._on_toggle_torch)
        self._refresh_torch_btn()
        top_box.pack_end(self.torch_btn, False, False, 0)
        root.pack_start(top_box, False, False, 0)

        # Notebook: Tab 1 = Xiaomi TV, Tab 2 = Flipper .ir Custom Remotes
        self.notebook = Gtk.Notebook()
        self.notebook.append_page(self._build_xiaomi_page(), Gtk.Label(label="小米电视"))
        self.notebook.append_page(self._build_custom_page(), Gtk.Label(label="自定义 .ir 码库"))
        root.pack_start(self.notebook, True, True, 0)

        # Status bar
        self.status_lbl = Gtk.Label(
            label="已预置小米电视 / 小米盒子红外协议 (38kHz)，点击按键即发射",
            xalign=0,
        )
        self.status_lbl.set_ellipsize(3)
        self.status_lbl.get_style_context().add_class("status-bar")
        root.pack_end(self.status_lbl, False, False, 0)

        self._load_saved_ir_files()

    def _apply_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_xiaomi_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(12)

        # Row 1: Power + Mute + Camera IR Check
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_power = self._make_xiaomi_btn("power", "⏻ 电源", css_class="power-btn", w=116, h=64)
        btn_mute = self._make_xiaomi_btn("mute", "🔇 静音", css_class="pill-btn", w=104, h=64)
        btn_test = Gtk.Button(label="红外测光")
        btn_test.set_size_request(104, 64)
        btn_test.get_style_context().add_class("pill-btn")
        btn_test.connect("clicked", self._on_ir_test)

        row1.pack_start(btn_power, True, True, 0)
        row1.pack_start(btn_mute, True, True, 0)
        row1.pack_start(btn_test, True, True, 0)
        box.pack_start(row1, False, False, 0)

        # Row 2: D-Pad Grid
        dpad_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        dpad_frame.get_style_context().add_class("dpad-frame")
        grid = Gtk.Grid(row_spacing=12, column_spacing=12)
        grid.set_row_homogeneous(True)
        grid.set_column_homogeneous(True)
        grid.set_size_request(-1, 270)

        grid.attach(self._make_xiaomi_btn("up", "▲", css_class="dpad-btn"), 1, 0, 1, 1)
        grid.attach(self._make_xiaomi_btn("left", "◀", css_class="dpad-btn"), 0, 1, 1, 1)
        grid.attach(self._make_xiaomi_btn("ok", "OK", css_class="ok-btn"), 1, 1, 1, 1)
        grid.attach(self._make_xiaomi_btn("right", "▶", css_class="dpad-btn"), 2, 1, 1, 1)
        grid.attach(self._make_xiaomi_btn("down", "▼", css_class="dpad-btn"), 1, 2, 1, 1)
        dpad_frame.pack_start(grid, True, True, 0)
        box.pack_start(dpad_frame, True, True, 0)

        # Row 3: Back / Home / Menu
        row3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row3.set_homogeneous(True)
        row3.pack_start(self._make_xiaomi_btn("back", "↩ 返回", css_class="pill-btn", h=64), True, True, 0)
        row3.pack_start(self._make_xiaomi_btn("home", "⌂ 主页", css_class="pill-btn", h=64), True, True, 0)
        row3.pack_start(self._make_xiaomi_btn("menu", "☰ 菜单", css_class="pill-btn", h=64), True, True, 0)
        box.pack_start(row3, False, False, 0)

        # Row 4: Volume − / Volume +
        row4 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        row4.set_homogeneous(True)
        row4.pack_start(self._make_xiaomi_btn("vol_down", "🔉 音量 −", css_class="pill-btn", h=70), True, True, 0)
        row4.pack_start(self._make_xiaomi_btn("vol_up", "🔊 音量 +", css_class="pill-btn", h=70), True, True, 0)
        box.pack_start(row4, False, False, 0)

        return box

    def _build_custom_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(10)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.ir_combo = Gtk.ComboBoxText()
        self.ir_combo.connect("changed", self._on_ir_file_selected)
        btn_import = Gtk.Button(label="导入 .ir 文件")
        btn_import.set_size_request(118, 44)
        btn_import.connect("clicked", self._on_import_ir)
        bar.pack_start(self.ir_combo, True, True, 0)
        bar.pack_end(btn_import, False, False, 0)
        box.pack_start(bar, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.custom_flow = Gtk.FlowBox()
        self.custom_flow.set_valign(Gtk.Align.START)
        self.custom_flow.set_max_children_per_line(3)
        self.custom_flow.set_min_children_per_line(2)
        self.custom_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.custom_flow.set_row_spacing(10)
        self.custom_flow.set_column_spacing(10)
        scroll.add(self.custom_flow)
        box.pack_start(scroll, True, True, 0)

        hint = Gtk.Label(
            label=f"支持 Flipper-IRDB 格式 (.ir)，文件保存在 {USER_IR_DIR}",
            xalign=0,
        )
        hint.get_style_context().add_class("header-sub")
        hint.set_line_wrap(True)
        box.pack_end(hint, False, False, 0)

        return box

    def _make_xiaomi_btn(self, key_id: str, label: str, css_class: str = "", w: int = -1, h: int = -1) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        if w > 0 or h > 0:
            btn.set_size_request(w, h)
        if css_class:
            btn.get_style_context().add_class(css_class)
        btn.connect("clicked", self._on_xiaomi_key, key_id)
        return btn

    def _set_status(self, text: str):
        GLib.idle_add(self.status_lbl.set_text, text)

    def _send_lirc_async(self, label: str, pulses: list[int], freq: int = 38000, duty: int = 33, info: str = ""):
        def worker():
            with self._tx_lock:
                try:
                    fd = os.open(LIRC_DEV, os.O_RDWR)
                    try:
                        fcntl.ioctl(fd, LIRC_SET_SEND_CARRIER, struct.pack("<I", freq))
                        fcntl.ioctl(fd, LIRC_SET_SEND_DUTY_CYCLE, struct.pack("<I", duty))
                        payload = struct.pack(f"<{len(pulses)}I", *pulses)
                        os.write(fd, payload)
                    finally:
                        os.close(fd)
                    extra = f" [{info}]" if info else ""
                    self._set_status(f"已发送: {label}{extra} ({freq // 1000}kHz)")
                except Exception as exc:
                    self._set_status(f"发送失败 ({label}): {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_xiaomi_key(self, _btn, key_id: str):
        name, code = XIAOMI_TV_KEYS[key_id]
        pulses = encode_xiaomi_tv(code, repeats=3)
        self._send_lirc_async(name, pulses, freq=38000, duty=33, info=f"0x{code:05x}")

    def _on_ir_test(self, _btn):
        # 0.8s continuous 38kHz carrier pulse stream so another phone camera can clearly see the purple IR glow
        pulses = [8000, 2000] * 40 + [8000]
        self._set_status("正在发射 0.8 秒连续红外测光波（用另一台手机相机对准顶部红外口可见紫光）...")
        self._send_lirc_async("红外测光", pulses, freq=38000, duty=50, info="800ms")

    def _read_torch_state(self) -> bool:
        try:
            with open(FLASH_LED_PATH, "r") as f:
                return int(f.read().strip()) > 0
        except Exception:
            return False

    def _refresh_torch_btn(self):
        ctx = self.torch_btn.get_style_context()
        if self._torch_on:
            ctx.add_class("torch-on")
            self.torch_btn.set_label("🔦 开")
        else:
            ctx.remove_class("torch-on")
            self.torch_btn.set_label("🔦 关")

    def _on_toggle_torch(self, _btn):
        target = 0 if self._torch_on else 120
        try:
            with open(FLASH_LED_PATH, "w") as f:
                f.write(str(target))
            self._torch_on = target > 0
            self._refresh_torch_btn()
            self._set_status(f"后置手电筒已{'开启 (亮度 120/255)' if self._torch_on else '关闭'}")
        except Exception as exc:
            self._set_status(f"手电筒切换失败: {exc}")

    def _load_saved_ir_files(self):
        self.ir_combo.remove_all()
        files = sorted(glob.glob(os.path.join(USER_IR_DIR, "*.ir")))
        for path in files:
            self.ir_combo.append(path, os.path.basename(path))
        if files:
            self.ir_combo.set_active(0)

    def _on_ir_file_selected(self, combo):
        path = combo.get_active_id()
        for child in self.custom_flow.get_children():
            self.custom_flow.remove(child)
        if not path or not os.path.isfile(path):
            return
        try:
            sigs = parse_flipper_ir(path)
        except Exception as exc:
            self._set_status(f"解析 .ir 失败: {exc}")
            return
        self._custom_signals = sigs
        for sig in sigs:
            btn = Gtk.Button(label=sig["name"])
            btn.set_size_request(110, 56)
            btn.connect("clicked", self._on_custom_sig, sig)
            self.custom_flow.add(btn)
        self.custom_flow.show_all()
        self._set_status(f"已加载 {os.path.basename(path)} ({len(sigs)} 个按键)")

    def _on_custom_sig(self, _btn, sig: dict):
        self._send_lirc_async(sig["name"], sig["pulses"], freq=sig["freq"], duty=sig["duty"], info=sig["info"])

    def _on_import_ir(self, _btn):
        dlg = Gtk.FileChooserDialog(
            title="选择 Flipper Zero .ir 红外码库文件",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,
            Gtk.ResponseType.OK,
        )
        flt = Gtk.FileFilter()
        flt.set_name("Flipper IR files (*.ir)")
        flt.add_pattern("*.ir")
        dlg.add_filter(flt)
        if dlg.run() == Gtk.ResponseType.OK:
            src = dlg.get_filename()
            dst = os.path.join(USER_IR_DIR, os.path.basename(src))
            try:
                if os.path.abspath(src) != os.path.abspath(dst):
                    with open(src, "rb") as fin, open(dst, "wb") as fout:
                        fout.write(fin.read())
                self._load_saved_ir_files()
                self.ir_combo.set_active_id(dst)
            except Exception as exc:
                self._set_status(f"导入失败: {exc}")
        dlg.destroy()


def main():
    win = PistaRemoteWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
