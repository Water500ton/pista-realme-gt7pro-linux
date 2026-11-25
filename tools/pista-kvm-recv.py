#!/usr/bin/env python3
"""
pista-kvm-recv.py — Zero-dependency Linux uinput gaming Keyboard + Mouse receiver.

Runs on Realme GT7 Pro (Debian ARM64) as root, binds to USB CDC NCM (172.16.42.1:17777),
and creates two native kernel input devices via /dev/uinput:
  - Pista USB Gaming Keyboard (full key hold, N-key rollover, combos)
  - Pista USB Gaming Mouse    (relative delta for FPS pointer lock, wheel, side buttons)

Usage:
  sudo python3 pista-kvm-recv.py [bind_ip] [port]
"""

import fcntl
import os
import signal
import socket
import struct
import sys

# Linux UAPI ioctl helpers (identical on x86_64 and aarch64)
_IOC_WRITE = 1
def _IOC(d, t, nr, sz):
    return (d << 30) | (sz << 16) | (ord(t) << 8) | nr

UI_DEV_CREATE  = _IOC(0, 'U', 1, 0)
UI_DEV_DESTROY = _IOC(0, 'U', 2, 0)
UI_DEV_SETUP   = _IOC(_IOC_WRITE, 'U', 3, 92)
UI_SET_EVBIT   = _IOC(_IOC_WRITE, 'U', 100, 4)
UI_SET_KEYBIT  = _IOC(_IOC_WRITE, 'U', 101, 4)
UI_SET_RELBIT  = _IOC(_IOC_WRITE, 'U', 102, 4)

BUS_USB = 0x03
EV_SYN  = 0x00
EV_KEY  = 0x01
EV_REL  = 0x02
EV_REP  = 0x14
SYN_REPORT = 0

REL_CODES = [0x00, 0x01, 0x06, 0x08, 0x0b, 0x0c]  # X, Y, HWHEEL, WHEEL, WHEEL_HI_RES, HWHEEL_HI_RES
MOUSE_BTNS = list(range(0x110, 0x118))             # BTN_LEFT .. BTN_TASK

PKT_FMT = "<BBHIi"  # dev_id(u8), flags(u8), type(u16), code(u32), value(i32)
PKT_SIZE = struct.calcsize(PKT_FMT)
EV_FMT = "<qqHHi"   # 64-bit struct input_event (24 bytes)

DEV_KBD = 0
DEV_MOUSE = 1
DEV_CTRL = 255
CTRL_RELEASE_ALL = 1


def create_uinput_keyboard():
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_REP)
    for code in range(1, 249):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    setup = struct.pack("<HHHH80sI", BUS_USB, 0x1D6B, 0x0101, 1, b"Pista USB Gaming Keyboard", 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def create_uinput_mouse():
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
    for btn in MOUSE_BTNS:
        fcntl.ioctl(fd, UI_SET_KEYBIT, btn)
    for rel in REL_CODES:
        fcntl.ioctl(fd, UI_SET_RELBIT, rel)
    setup = struct.pack("<HHHH80sI", BUS_USB, 0x1D6B, 0x0102, 1, b"Pista USB Gaming Mouse", 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit(fd, ev_type, ev_code, ev_val):
    os.write(fd, struct.pack(EV_FMT, 0, 0, ev_type, ev_code, ev_val))


def release_all(kbd_fd, mouse_fd, held_kbd, held_mouse):
    if held_kbd:
        for code in list(held_kbd):
            emit(kbd_fd, EV_KEY, code, 0)
        emit(kbd_fd, EV_SYN, SYN_REPORT, 0)
        held_kbd.clear()
    if held_mouse:
        for code in list(held_mouse):
            emit(mouse_fd, EV_KEY, code, 0)
        emit(mouse_fd, EV_SYN, SYN_REPORT, 0)
        held_mouse.clear()


def recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def main():
    bind_ip = sys.argv[1] if len(sys.argv) > 1 else "172.16.42.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 17777

    kbd_fd = create_uinput_keyboard()
    mouse_fd = create_uinput_mouse()
    held_kbd = set()
    held_mouse = set()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, port))
    srv.listen(1)

    def cleanup(*_):
        release_all(kbd_fd, mouse_fd, held_kbd, held_mouse)
        for fd in (kbd_fd, mouse_fd):
            try:
                fcntl.ioctl(fd, UI_DEV_DESTROY)
                os.close(fd)
            except OSError:
                pass
        try:
            srv.close()
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print(f"[pista-kvm-recv] Virtual Gaming Keyboard + Mouse ready on {bind_ip}:{port}", flush=True)

    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[pista-kvm-recv] Connected from {addr[0]}:{addr[1]}", flush=True)
        try:
            while True:
                data = recv_exact(conn, PKT_SIZE)
                if not data:
                    break
                dev_id, _flags, ev_type, ev_code, ev_val = struct.unpack(PKT_FMT, data)
                if dev_id == DEV_CTRL:
                    if ev_code == CTRL_RELEASE_ALL:
                        release_all(kbd_fd, mouse_fd, held_kbd, held_mouse)
                elif dev_id == DEV_KBD:
                    if ev_type == EV_KEY:
                        if ev_val == 1:
                            held_kbd.add(ev_code)
                        elif ev_val == 0:
                            held_kbd.discard(ev_code)
                    emit(kbd_fd, ev_type, ev_code, ev_val)
                elif dev_id == DEV_MOUSE:
                    if ev_type == EV_KEY:
                        if ev_val == 1:
                            held_mouse.add(ev_code)
                        elif ev_val == 0:
                            held_mouse.discard(ev_code)
                    emit(mouse_fd, ev_type, ev_code, ev_val)
        except OSError:
            pass
        finally:
            release_all(kbd_fd, mouse_fd, held_kbd, held_mouse)
            conn.close()
            print("[pista-kvm-recv] Disconnected (all keys released)", flush=True)


if __name__ == "__main__":
    main()
