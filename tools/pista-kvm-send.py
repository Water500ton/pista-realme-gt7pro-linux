#!/usr/bin/env python3
"""
pista-kvm-send.py — Zero-dependency Linux evdev Keyboard + Mouse sender over USB.

Runs on the Linux PC (with sudo), auto-detects physical keyboards and mice,
and forwards raw evdev events over TCP (TCP_NODELAY) to pista-kvm-recv.py on the phone.

Toggle PC <-> Phone capture at any time with:
  - ScrollLock   (single tap), OR
  - Right Ctrl + Right Alt

Usage:
  sudo python3 pista-kvm-send.py [phone_ip] [port]
"""

import fcntl
import glob
import os
import select
import signal
import socket
import struct
import sys

_IOC_WRITE = 1
_IOC_READ = 2
def _IOC(d, t, nr, sz):
    return (d << 30) | (sz << 16) | (ord(t) << 8) | nr

EVIOCGRAB = _IOC(_IOC_WRITE, 'E', 0x90, 4)
def EVIOCGNAME(length):
    return _IOC(_IOC_READ, 'E', 0x06, length)
def EVIOCGBIT(ev, length):
    return _IOC(_IOC_READ, 'E', 0x20 + ev, length)

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02

KEY_A = 30
KEY_SPACE = 57
KEY_SCROLLLOCK = 70
KEY_RIGHTCTRL = 97
KEY_RIGHTALT = 100
BTN_LEFT = 0x110
REL_X = 0x00
REL_Y = 0x01

PKT_FMT = "<BBHIi"
EV_FMT = "<qqHHi"
EV_SIZE = struct.calcsize(EV_FMT)

DEV_KBD = 0
DEV_MOUSE = 1
DEV_CTRL = 255
CTRL_RELEASE_ALL = 1


def has_bit(buf, bit):
    idx = bit // 8
    return idx < len(buf) and bool(buf[idx] & (1 << (bit % 8)))


def get_dev_name(fd):
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, EVIOCGNAME(256), buf)
        return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")
    except OSError:
        return "unknown"


def discover_devices():
    kbds, mice = [], []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        name = get_dev_name(fd)
        lname = name.lower()
        if "pista usb gaming" in lname or "virtual" in lname:
            os.close(fd)
            continue

        evbits = bytearray(4)
        keybits = bytearray(96)
        relbits = bytearray(4)
        try:
            fcntl.ioctl(fd, EVIOCGBIT(0, len(evbits)), evbits)
            if has_bit(evbits, EV_KEY):
                fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, len(keybits)), keybits)
            if has_bit(evbits, EV_REL):
                fcntl.ioctl(fd, EVIOCGBIT(EV_REL, len(relbits)), relbits)
        except OSError:
            os.close(fd)
            continue

        is_kbd = has_bit(keybits, KEY_A) and has_bit(keybits, KEY_SPACE)
        is_mouse = has_bit(evbits, EV_REL) and has_bit(relbits, REL_X) and has_bit(relbits, REL_Y) and has_bit(keybits, BTN_LEFT)

        if is_kbd or is_mouse:
            if is_kbd:
                kbds.append((fd, path, name))
            if is_mouse and not is_kbd:
                mice.append((fd, path, name))
        else:
            os.close(fd)
    return kbds, mice


def set_grab(devices, grab):
    val = 1 if grab else 0
    for fd, _path, _name in devices:
        try:
            fcntl.ioctl(fd, EVIOCGRAB, val)
        except OSError:
            pass


def main():
    if os.geteuid() != 0:
        print("Run with sudo so /dev/input/event* can be grabbed cleanly.", file=sys.stderr)
        sys.exit(1)

    phone_ip = sys.argv[1] if len(sys.argv) > 1 else "172.16.42.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 17777

    kbds, mice = discover_devices()
    if not kbds and not mice:
        print("No physical keyboards or mice found under /dev/input/event*", file=sys.stderr)
        sys.exit(1)

    print("Detected keyboards:")
    for _, p, n in kbds:
        print(f"  [KBD]   {p}: {n}")
    print("Detected mice:")
    for _, p, n in mice:
        print(f"  [MOUSE] {p}: {n}")

    sock = socket.create_connection((phone_ip, port), timeout=5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"\nConnected to phone {phone_ip}:{port}!")
    print("Press [ScrollLock] OR [Right Ctrl + Right Alt] to toggle PC <-> PHONE gaming capture.")
    print("Press Ctrl+C (when on PC) to quit.\n", flush=True)

    all_devs = kbds + mice
    fd_to_dev = {}
    for fd, _, _ in kbds:
        fd_to_dev[fd] = DEV_KBD
    for fd, _, _ in mice:
        fd_to_dev[fd] = DEV_MOUSE

    active = False
    pressed_mods = set()
    pending_toggle_combo = False
    pending_toggle_scroll = False

    def switch_mode(new_active):
        nonlocal active
        if new_active == active:
            return
        if not new_active:
            try:
                sock.sendall(struct.pack(PKT_FMT, DEV_CTRL, 0, 0, CTRL_RELEASE_ALL, 0))
            except OSError:
                pass
            set_grab(all_devs, False)
            active = False
            print(">>> Control: PC (local)", flush=True)
        else:
            set_grab(all_devs, True)
            active = True
            print(">>> Control: PHONE (Realme GT7 Pro — gaming passthrough ACTIVE!)", flush=True)

    def cleanup(*_):
        switch_mode(False)
        try:
            sock.close()
        except OSError:
            pass
        for fd, _, _ in all_devs:
            try:
                os.close(fd)
            except OSError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        while True:
            rlist, _, _ = select.select(list(fd_to_dev.keys()), [], [])
            for fd in rlist:
                try:
                    raw = os.read(fd, EV_SIZE * 64)
                except BlockingIOError:
                    continue
                if not raw:
                    continue

                out = bytearray()
                syn_devs = set()
                for off in range(0, len(raw) - (len(raw) % EV_SIZE), EV_SIZE):
                    _sec, _usec, ev_type, ev_code, ev_val = struct.unpack_from(EV_FMT, raw, off)

                    # Hotkey detection (toggle on release so PC never sees a stuck key!)
                    if ev_type == EV_KEY:
                        if ev_code == KEY_SCROLLLOCK:
                            if ev_val == 1:
                                pending_toggle_scroll = True
                            elif ev_val == 0 and pending_toggle_scroll:
                                pending_toggle_scroll = False
                                switch_mode(not active)
                            continue
                        if ev_code in (KEY_RIGHTCTRL, KEY_RIGHTALT):
                            if ev_val in (1, 2):
                                pressed_mods.add(ev_code)
                                if KEY_RIGHTCTRL in pressed_mods and KEY_RIGHTALT in pressed_mods:
                                    pending_toggle_combo = True
                                    continue
                            elif ev_val == 0:
                                pressed_mods.discard(ev_code)
                                if pending_toggle_combo and not pressed_mods:
                                    pending_toggle_combo = False
                                    switch_mode(not active)
                                    continue

                    if not active:
                        continue

                    if ev_type == EV_REL or (ev_type == EV_KEY and 0x110 <= ev_code <= 0x117):
                        out += struct.pack(PKT_FMT, DEV_MOUSE, 0, ev_type, ev_code, ev_val)
                        syn_devs.add(DEV_MOUSE)
                    elif ev_type == EV_KEY and 1 <= ev_code <= 248:
                        out += struct.pack(PKT_FMT, DEV_KBD, 0, ev_type, ev_code, ev_val)
                        syn_devs.add(DEV_KBD)
                    elif ev_type == EV_SYN:
                        for d in (syn_devs or {fd_to_dev[fd]}):
                            out += struct.pack(PKT_FMT, d, 0, ev_type, ev_code, ev_val)
                        syn_devs.clear()

                if out:
                    sock.sendall(out)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
