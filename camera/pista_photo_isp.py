#!/usr/bin/env python3
"""Computational Photography Still-Capture ISP Engine for Realme GT7 Pro (SM8750 / pista).

Ports core smartphone ISP stages using Realme GT7 Pro optical profiles & factory EEPROM data:
  - Lucky-frame + FFT phase-correlation burst alignment
  - Multi-Frame Noise Reduction (MFNR) with shot-noise-adaptive Tukey motion rejection
  - Multi-Exposure Bracketing HDR via 4-level Laplacian Pyramid Mertens Exposure Fusion
  - 17x13 RGB Lens Shading Correction (LSC) in linear light with SNR-adaptive corner cap
  - Multi-scale Local Tone Mapping (LTM) + Filmic S-curve + Skin-safe Vibrance
  - YCbCr edge-preserving chroma denoise + halo-free cored multi-scale sharpening (ASF)
  - Standard smartphone EXIF embedding (Make/Model/Lens/FocalLength/FNumber/ISO/Exposure/Focus)
"""

import datetime
import fcntl
import glob
import json
import math
import os
import struct
import subprocess
import time

import numpy as np
from PIL import Image

V4L2_CID_EXPOSURE = 0x00980911
V4L2_CID_VBLANK = 0x009E0901
V4L2_CID_ANALOGUE_GAIN = 0x009E0903
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_QUERYCTRL = 0xC0445624

ISP_PROFILE_PATHS = [
    "/etc/pista-camera-isp.json",
    "/usr/local/share/pista-camera/pista_isp_profiles.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pista_isp_profiles.json"),
]


def load_isp_profiles():
    merged = {}
    # Load built-in/installed golden profiles first, then overlay per-unit EEPROM overrides
    for path in reversed(ISP_PROFILE_PATHS):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if k not in merged:
                        merged[k] = dict(v)
                    else:
                        merged[k].update(v)
            except Exception:
                pass
    return merged


def read_eeprom_lsc_17x13(bus: int, addr: int = 0x50, base_off: int = 0x00D0):
    """Read Sunny/Oplus 17x13x4 10-bit packed LSC mesh via i2ctransfer.

    Sunny GT24P128G packs 51 grid points (255 B) per 256-byte EEPROM page with
    1 padding byte (0xFF) at offset 255 of each page: 4 * 51 + 17 = 221 points
    spanning 0x00D0 .. 0x0524 (1109 bytes total).
    Qualcomm CCI adapter max_read_len is 12 bytes, so read in 10-byte chunks.
    Packing per grid point (5 bytes):
      R  = ((b[4] & 0xC0) << 2) | b[0]
      Gr = ((b[4] & 0x30) << 4) | b[1]
      Gb = ((b[4] & 0x0C) << 6) | b[2]
      B  = ((b[4] & 0x03) << 8) | b[3]
    """
    total_span = 4 * 256 + 17 * 5  # 1109 bytes (0x00D0 .. 0x0524)
    raw = bytearray()
    off = base_off
    while len(raw) < total_span:
        chunk = min(10, total_span - len(raw))
        hi = (off >> 8) & 0xFF
        lo = off & 0xFF
        r = subprocess.run(
            ["i2ctransfer", "-y", str(bus), f"w2@{hex(addr)}", hex(hi), hex(lo), f"r{chunk}"],
            capture_output=True,
            text=True,
            check=True,
        )
        raw.extend(int(tok, 16) for tok in r.stdout.strip().split())
        off += chunk

    r_vals, g_vals, b_vals = [], [], []
    for k in range(221):
        pos = (k // 51) * 256 + (k % 51) * 5
        b0, b1, b2, b3, b4 = raw[pos : pos + 5]
        rv = ((b4 & 0xC0) << 2) | b0
        gr = ((b4 & 0x30) << 4) | b1
        gb = ((b4 & 0x0C) << 6) | b2
        bv = ((b4 & 0x03) << 8) | b3
        r_vals.append(max(1.0, float(rv)))
        g_vals.append(max(1.0, 0.5 * (gr + gb)))
        b_vals.append(max(1.0, float(bv)))

    # EEPROM stores raw relative sensitivity (center bright, corners dark) -> invert to gain mesh!
    rc, gc, bc = r_vals[110], g_vals[110], b_vals[110]
    if rc < 200 or gc < 200 or bc < 200:
        raise ValueError("Invalid EEPROM LSC center values")
    r_gain = [round(max(0.85, min(6.5, rc / v)), 3) for v in r_vals]
    g_gain = [round(max(0.85, min(6.5, gc / v)), 3) for v in g_vals]
    b_gain = [round(max(0.85, min(6.5, bc / v)), 3) for v in b_vals]

    # Sanity check: 4 corners must be symmetric & brighter gain than center
    g_corners = [g_gain[0], g_gain[16], g_gain[204], g_gain[220]]
    if min(g_corners) < 1.15 or (max(g_corners) / min(g_corners)) > 1.45:
        raise ValueError(f"EEPROM LSC corners asymmetric: {g_corners}")
    return r_gain, g_gain, b_gain


def find_subdev_by_prefix(prefix: str):
    if not prefix:
        return None
    for path in sorted(glob.glob("/sys/class/video4linux/v4l-subdev*/name")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read().strip().startswith(prefix):
                    return f"/dev/{os.path.basename(os.path.dirname(path))}"
        except OSError:
            continue
    return None


class SensorControlHelper:
    """Reads/adjusts live V4L2 exposure & analogue gain for HDR bracketing and EXIF."""

    def __init__(self, sensor_prefix: str):
        self.dev_path = find_subdev_by_prefix(sensor_prefix)

    def _get_ctrl(self, cid: int):
        if not self.dev_path:
            return None
        try:
            fd = os.open(self.dev_path, os.O_RDWR)
            try:
                buf = bytearray(struct.pack("Ii", cid, 0))
                fcntl.ioctl(fd, VIDIOC_G_CTRL, buf)
                _, val = struct.unpack("Ii", buf)
                return val
            finally:
                os.close(fd)
        except OSError:
            return None

    def _set_ctrl(self, cid: int, val: int) -> bool:
        if not self.dev_path:
            return False
        try:
            fd = os.open(self.dev_path, os.O_RDWR)
            try:
                fcntl.ioctl(fd, VIDIOC_S_CTRL, struct.pack("Ii", cid, int(val)))
                return True
            finally:
                os.close(fd)
        except OSError:
            return False

    def _query_range(self, cid: int):
        if not self.dev_path:
            return (4, 4000)
        try:
            fd = os.open(self.dev_path, os.O_RDWR)
            try:
                buf = bytearray(68)
                struct.pack_into("I", buf, 0, cid)
                fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
                cmin, cmax = struct.unpack_from("ii", buf, 40)
                return (cmin, cmax)
            finally:
                os.close(fd)
        except OSError:
            return (4, 4000)

    def get_exposure(self):
        return self._get_ctrl(V4L2_CID_EXPOSURE)

    def get_gain(self):
        return self._get_ctrl(V4L2_CID_ANALOGUE_GAIN)

    def get_exposure_range(self):
        return self._query_range(V4L2_CID_EXPOSURE)

    def get_gain_range(self):
        return self._query_range(V4L2_CID_ANALOGUE_GAIN)

    def set_exposure(self, val: int) -> bool:
        lo, hi = self.get_exposure_range()
        return self._set_ctrl(V4L2_CID_EXPOSURE, max(lo, min(hi, int(val))))

    def set_gain(self, val: int) -> bool:
        lo, hi = self.get_gain_range()
        return self._set_ctrl(V4L2_CID_ANALOGUE_GAIN, max(lo, min(hi, int(val))))

    def set_vblank(self, val: int) -> bool:
        lo, hi = self._query_range(V4L2_CID_VBLANK)
        return self._set_ctrl(V4L2_CID_VBLANK, max(lo, min(hi, int(val))))

    def ensure_30fps_vblank(self, sensor_key: str):
        """Raise VBLANK on 60fps-default sensors (IMX355/IMX471) to unlock 33ms (~30fps) max shutter."""
        # IMX355 (1632x1224) defaults to max_exp=1296 (~16ms); vblank=1400 unlocks ~2600 lines (~33ms, +1 EV).
        # IMX471 (1920x1080) defaults to max_exp=1290 (~18ms); vblank=1400 unlocks ~2470 lines (~33ms, +1 EV).
        if sensor_key in ("rear_uw", "front"):
            _lo, hi = self.get_exposure_range()
            if hi < 2000:
                self.set_vblank(1400)

    def set_exposure_and_gain(self, exp_val: int, gain_val: int):
        if exp_val is not None:
            self.set_exposure(exp_val)
        if gain_val is not None:
            self.set_gain(gain_val)

    @staticmethod
    def smia_base(sensor_key: str) -> float:
        return 16384.0 if sensor_key in ("rear_main", "rear_tele") else 1024.0

    def gain_code_to_lin(self, sensor_key: str, code: int) -> float:
        base = self.smia_base(sensor_key)
        min_denom = 256.0 if base > 2048.0 else 32.0
        return base / max(min_denom, base - float(code))

    def gain_lin_to_code(self, sensor_key: str, gain_lin: float) -> int:
        base = self.smia_base(sensor_key)
        g_lo, g_hi = self.get_gain_range()
        g = max(1.0, float(gain_lin))
        code = int(round(base - (base / g)))
        return max(g_lo, min(g_hi, code))

    def total_ev_to_regs(self, sensor_key: str, total_ev: float, max_gain_lin: float = None):
        """Decompose total linear exposure (exp_lines * gain_lin) into optimal (exp_lines, gain_code).

        Shutter-priority: uses full exposure up to 1/30s (exp_hi) at 1.0x gain before raising
        analog gain, minimizing sensor read noise and preventing unnecessary high-ISO clipping.
        """
        exp_lo, exp_hi = self.get_exposure_range()
        g_lo, g_hi = self.get_gain_range()
        max_g = self.gain_code_to_lin(sensor_key, g_hi)
        if max_gain_lin is not None:
            max_g = min(max_g, float(max_gain_lin))
        total_ev = max(float(exp_lo), min(float(exp_hi) * max_g, float(total_ev)))
        if total_ev <= float(exp_hi):
            return (max(exp_lo, int(round(total_ev))), g_lo)
        req_g = total_ev / float(exp_hi)
        return (exp_hi, self.gain_lin_to_code(sensor_key, req_g))

    def compute_ev_bracket(self, sensor_key: str, orig_exp: int, orig_gain: int, mult: float):
        """Compute (exp_lines, gain_code) for an EV multiplier using true Sony SMIA total exposure."""
        cur_g = self.gain_code_to_lin(sensor_key, orig_gain) if orig_gain is not None else 1.0
        total = float(orig_exp) * cur_g * float(mult)
        return self.total_ev_to_regs(sensor_key, total)


class SmartAEController:
    """Sony-SMIA closed-loop highlight-protected Auto Exposure controller for pista-camera.

    Replaces libcamera SoftISP's naive linear-gain Agc (which lacks Sony CameraSensorHelpers,
    causing IMX906 screen blowouts and IMX355/IMX882 sluggish underexposure).
    """

    def __init__(self):
        self.sensor_key = None
        self.sc = None
        self.roi = (0.5, 0.5)   # normalized (cx, cy), updated by Tap-to-Focus/Meter
        self.ev_bias = 0.0      # EV compensation in stops
        self.wait_frames = 0
        self.cur_exp = None
        self.cur_gain = None
        self.initialized = False
        self.vblank_checked = False

    def configure(self, sensor_key: str, sensor_ctrl: SensorControlHelper):
        self.sensor_key = sensor_key
        self.sc = sensor_ctrl
        self.roi = (0.5, 0.5)
        self.ev_bias = 0.0
        self.wait_frames = 2
        self.cur_exp = None
        self.cur_gain = None
        self.initialized = False
        self.vblank_checked = False

    def set_metering_point(self, nx: float, ny: float):
        self.roi = (max(0.15, min(0.85, float(nx))), max(0.15, min(0.85, float(ny))))
        self.wait_frames = 0  # respond immediately to tap-to-meter

    def reset_metering_point(self):
        self.roi = (0.5, 0.5)

    def _init_reasonable_start(self):
        if not self.sc or not self.sensor_key:
            return
        if not self.vblank_checked:
            self.sc.ensure_30fps_vblank(self.sensor_key)
            self.vblank_checked = True
        exp_lo, exp_hi = self.sc.get_exposure_range()
        # Start near mid-shutter, moderate 2x gain so neither bright screens nor dim rooms start extreme
        init_exp = max(exp_lo, min(exp_hi, int(exp_hi * 0.45)))
        init_gain = self.sc.gain_lin_to_code(self.sensor_key, 2.0)
        self.sc.set_exposure_and_gain(init_exp, init_gain)
        self.cur_exp, self.cur_gain = init_exp, init_gain
        self.initialized = True
        self.wait_frames = 2

    def process_frame(self, arr_u8: np.ndarray, fmt: str):
        """Run one highlight-protected AE tick on a preview frame (<0.3 ms CPU)."""
        if not self.sc or not self.sensor_key:
            return
        if not self.initialized:
            self._init_reasonable_start()
            return
        if self.wait_frames > 0:
            self.wait_frames -= 1
            return

        # Subsample 16x for ultra-fast metering (~120x90 grid)
        sub = arr_u8[::16, ::16, :3]
        if fmt in ("BGRA", "BGRx", "BGR"):
            r = sub[:, :, 2].astype(np.float32)
            g = sub[:, :, 1].astype(np.float32)
            b = sub[:, :, 0].astype(np.float32)
        else:
            r = sub[:, :, 0].astype(np.float32)
            g = sub[:, :, 1].astype(np.float32)
            b = sub[:, :, 2].astype(np.float32)

        # Luminance & max-channel highlight map (max channel catches single-color screen clipping)
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        peak = np.maximum(np.maximum(r, g), b)
        sh, sw = luma.shape
        if sh < 8 or sw < 8:
            return

        # Metering ROI (36% box around center or tapped point)
        cx = int(self.roi[0] * sw)
        cy = int(self.roi[1] * sh)
        rx = max(4, int(sw * 0.18))
        ry = max(4, int(sh * 0.18))
        y0, y1 = max(0, cy - ry), min(sh, cy + ry)
        x0, x1 = max(0, cx - rx), min(sw, cx + rx)

        roi_luma = luma[y0:y1, x0:x1]
        roi_peak = peak[y0:y1, x0:x1]

        # Weighted scene luminance: 65% ROI + 35% global
        w_luma = 0.65 * float(np.mean(roi_luma)) + 0.35 * float(np.mean(luma))
        roi_p96 = float(np.percentile(roi_peak, 96))
        clip_frac = float(np.mean(roi_peak >= 246.0))
        global_clip = float(np.mean(peak >= 248.0))

        # Target middle gray in sRGB (~120/255: crisp, bright indoor exposure with highlight guard)
        target = 120.0 * (2.0 ** (self.ev_bias * 0.45))

        # 1. Base exposure ratio (convert sRGB luminance ratio to linear light via gamma ~1.9)
        meas = max(4.0, w_luma)
        ratio = (target / meas) ** 1.85

        # 2. Highlight / Screen Protection:
        # If bright screen/light in ROI or frame is clipping while scene isn't pitch-black,
        # pull exposure down so screens stay crisp & legible instead of blowing out to solid white.
        if (clip_frac > 0.035 or global_clip > 0.08 or roi_p96 > 244.0) and w_luma > 34.0:
            hl_ratio = (224.0 / max(224.0, roi_p96)) ** 2.1
            if clip_frac > 0.12:
                hl_ratio = min(hl_ratio, 0.55)
            elif clip_frac > 0.05:
                hl_ratio = min(hl_ratio, 0.72)
            # Don't crush overall scene darker than luma ~42 just for a tiny specular lamp
            min_safe_ratio = max(0.20, (42.0 / max(10.0, w_luma)) ** 1.85)
            ratio = min(ratio, max(hl_ratio, min_safe_ratio))

        # Deadband: avoid micro-hunting / unnecessary I2C writes when already well-exposed
        # (libpista_ae_guard.so prevents libcamera DelayedControls from overwriting our regs)
        if 0.89 <= ratio <= 1.12:
            return

        # Damped step in log-exposure space for smooth, rapid convergence (3-5 frames)
        log_step = math.log2(max(0.18, min(5.5, ratio)))
        damped_mult = 2.0 ** (log_step * 0.58)

        # Read current hardware regs (or use cached)
        exp_now = self.cur_exp if self.cur_exp is not None else self.sc.get_exposure()
        gain_now = self.cur_gain if self.cur_gain is not None else self.sc.get_gain()
        if exp_now is None or gain_now is None:
            return

        cur_g_lin = self.sc.gain_code_to_lin(self.sensor_key, gain_now)
        cur_total = float(exp_now) * cur_g_lin
        new_total = cur_total * damped_mult

        # Cap max analog gain per sensor to keep indoor shadows clean
        max_g_cap = {"rear_main": 24.0, "rear_tele": 28.0, "rear_uw": 15.0, "front": 4.57}.get(self.sensor_key, 16.0)
        new_exp, new_gain = self.sc.total_ev_to_regs(self.sensor_key, new_total, max_gain_lin=max_g_cap)

        if new_exp != exp_now or new_gain != gain_now:
            self.sc.set_exposure_and_gain(new_exp, new_gain)
            self.cur_exp, self.cur_gain = new_exp, new_gain
            # Wait 2 frames for sensor+SoftISP pipeline latency before next measurement
            self.wait_frames = 2

    def estimate_exif_params(self, sensor_key: str):
        """Convert V4L2 exposure lines and Sony SMIA gain code to approximate shutter & ISO."""
        exp_lines = self.get_exposure()
        gain_code = self.get_gain()
        # Approximate line time (us) per 30fps mode
        line_us_map = {
            "rear_main": 8.6,   # IMX906 ~3850 lines/33.3ms
            "rear_tele": 8.6,   # IMX882 ~3850 lines/33.3ms
            "rear_uw": 12.8,    # IMX355 ~2600 lines/33.3ms
            "front": 14.5,      # IMX471 ~1150 lines/17.3ms
        }
        line_us = line_us_map.get(sensor_key, 10.0)
        exp_sec = (exp_lines * line_us * 1e-6) if exp_lines and exp_lines > 0 else (1.0 / 60.0)
        exp_sec = max(1e-4, min(0.5, exp_sec))

        # Sony SMIA gain: G = 1024 / (1024 - code) for 10-bit, or 16384/(16384-code) for 14-bit
        iso = 100
        if gain_code is not None and gain_code >= 0:
            if sensor_key in ("rear_main", "rear_tele"):
                # 14-bit Sony IMX906/IMX882: base ISO 50
                denom = max(256.0, 16384.0 - float(gain_code))
                iso = int(round(50.0 * (16384.0 / denom)))
            else:
                denom = max(32.0, 1024.0 - float(gain_code))
                iso = int(round(100.0 * (1024.0 / denom)))
        iso = max(50, min(12800, iso))
        return exp_sec, iso


# ---------------------------------------------------------------------------
# Fast Vectorized Image Primitives (Pure NumPy + Pillow, Zero OpenCV Needed)
# ---------------------------------------------------------------------------

def _box_blur_1d(arr: np.ndarray, radius: int, axis: int) -> np.ndarray:
    if radius <= 0:
        return arr
    pad_w = [(0, 0)] * arr.ndim
    pad_w[axis] = (radius, radius)
    padded = np.pad(arr, pad_w, mode="edge")
    cs = np.cumsum(padded, axis=axis, dtype=np.float32)
    win = 2 * radius + 1
    sl_hi = [slice(None)] * arr.ndim
    sl_lo = [slice(None)] * arr.ndim
    sl_hi[axis] = slice(win, None)
    sl_lo[axis] = slice(None, -win)
    # Prepend zeros along axis for exact cumsum difference
    zshape = list(padded.shape)
    zshape[axis] = 1
    cs0 = np.concatenate([np.zeros(zshape, dtype=np.float32), cs], axis=axis)
    return (cs0[tuple(sl_hi)] - cs0[tuple(sl_lo)]) / float(win)


def _fast_gaussian_approx(arr: np.ndarray, radius: int) -> np.ndarray:
    """2-pass separable box blur (~Gaussian) in pure NumPy."""
    out = _box_blur_1d(arr, radius, axis=0)
    out = _box_blur_1d(out, radius, axis=1)
    out = _box_blur_1d(out, radius, axis=0)
    out = _box_blur_1d(out, radius, axis=1)
    return out


def _resize_float_plane(plane: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    im = Image.fromarray(plane.astype(np.float32), mode="F")
    im = im.resize((out_w, out_h), Image.Resampling.BILINEAR)
    return np.asarray(im, dtype=np.float32)


def _downsample_2x(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    h2 = (h // 2) * 2
    w2 = (w // 2) * 2
    sub = img[:h2, :w2]
    return 0.25 * (sub[0::2, 0::2] + sub[0::2, 1::2] + sub[1::2, 0::2] + sub[1::2, 1::2])


def _upsample_to(img: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    if img.ndim == 2:
        return _resize_float_plane(img, target_w, target_h)
    chans = [_resize_float_plane(img[:, :, c], target_w, target_h) for c in range(img.shape[2])]
    return np.stack(chans, axis=2)


def _frame_sharpness(rgb_u8: np.ndarray) -> float:
    """Fast center-ROI Tenengrad sharpness for lucky-frame reference selection."""
    h, w, _ = rgb_u8.shape
    g = rgb_u8[h // 4 : (3 * h) // 4 : 4, w // 4 : (3 * w) // 4 : 4, 1].astype(np.float32)
    gx = g[:, 2:] - g[:, :-2]
    gy = g[2:, :] - g[:-2, :]
    return float(np.mean(gx * gx) + np.mean(gy * gy))


def align_shift_fft(ref_rgb: np.ndarray, mov_rgb: np.ndarray, max_shift: int = 44):
    """Estimate integer (dy, dx) shift using gradient-based FFT phase correlation.

    Using gradient magnitude makes alignment invariant to EV brightness differences
    (works equally well for MFNR bursts and EV-bracketed HDR frames).
    """
    # 4x downsampled Green channel
    r = ref_rgb[::4, ::4, 1].astype(np.float32)
    m = mov_rgb[::4, ::4, 1].astype(np.float32)
    # Gradient magnitude removes DC and exposure scaling
    rg = np.abs(r[1:-1, 2:] - r[1:-1, :-2]) + np.abs(r[2:, 1:-1] - r[:-2, 1:-1])
    mg = np.abs(m[1:-1, 2:] - m[1:-1, :-2]) + np.abs(m[2:, 1:-1] - m[:-2, 1:-1])
    rg -= rg.mean()
    mg -= mg.mean()

    h, w = rg.shape
    win = np.outer(np.hanning(h).astype(np.float32), np.hanning(w).astype(np.float32))
    F1 = np.fft.rfft2(rg * win)
    F2 = np.fft.rfft2(mg * win)
    cross = F1 * np.conj(F2)
    mag = np.abs(cross) + 1e-6
    corr = np.fft.irfft2(cross / mag, s=(h, w))

    # Restrict search window to +/- max_shift/4 in downsampled coords
    lim = max(2, max_shift // 4)
    # Roll so (0, 0) is at center (lim, lim)
    c_shift = np.roll(np.roll(corr, lim, axis=0), lim, axis=1)[: 2 * lim + 1, : 2 * lim + 1]
    iy, ix = np.unravel_index(int(np.argmax(c_shift)), c_shift.shape)
    dy = (iy - lim) * 4
    dx = (ix - lim) * 4
    return int(dy), int(dx)


def shift_image(img: np.ndarray, dy: int, dx: int) -> np.ndarray:
    if dy == 0 and dx == 0:
        return img
    h, w = img.shape[:2]
    out = np.empty_like(img)
    # Clamp edge pixels instead of wrapping around
    y_src0 = max(0, -dy)
    y_src1 = min(h, h - dy)
    y_dst0 = max(0, dy)
    y_dst1 = min(h, h + dy)
    x_src0 = max(0, -dx)
    x_src1 = min(w, w - dx)
    x_dst0 = max(0, dx)
    x_dst1 = min(w, w + dx)
    out[y_dst0:y_dst1, x_dst0:x_dst1] = img[y_src0:y_src1, x_src0:x_src1]
    if y_dst0 > 0:
        out[:y_dst0, :] = out[y_dst0 : y_dst0 + 1, :]
    if y_dst1 < h:
        out[y_dst1:, :] = out[y_dst1 - 1 : y_dst1, :]
    if x_dst0 > 0:
        out[:, :x_dst0] = out[:, x_dst0 : x_dst0 + 1]
    if x_dst1 < w:
        out[:, x_dst1:] = out[:, x_dst1 - 1 : x_dst1]
    return out


# ---------------------------------------------------------------------------
# Stage 1A: Multi-Frame Noise Reduction (MFNR — Qualcomm mf132 / HDR+ Style)
# ---------------------------------------------------------------------------

def merge_mfnr_linear(frames_u8: list) -> np.ndarray:
    """Align & merge a burst of RGB uint8 frames in linear light with motion rejection."""
    if len(frames_u8) == 1:
        return np.power(frames_u8[0].astype(np.float32) * (1.0 / 255.0), 2.2)

    # Lucky imaging: pick sharpest frame as reference
    scores = [_frame_sharpness(f) for f in frames_u8]
    ref_idx = int(np.argmax(scores))
    ref_u8 = frames_u8[ref_idx]

    # Convert reference to linear light [0..1]
    inv255 = np.float32(1.0 / 255.0)
    ref_srgb = ref_u8.astype(np.float32) * inv255
    ref_lin = np.power(ref_srgb, 2.2)
    ref_luma = 0.299 * ref_srgb[:, :, 0] + 0.587 * ref_srgb[:, :, 1] + 0.114 * ref_srgb[:, :, 2]

    # Shot-noise-adaptive motion rejection threshold (stronger tolerance in dark shadows)
    # In sRGB domain, noise std ~ 0.025..0.06
    tau = np.clip(0.045 + 0.045 * (1.0 - ref_luma), 0.04, 0.095).astype(np.float32)
    inv_tau2 = 1.0 / (tau * tau)

    accum = ref_lin.copy()
    wsum = np.ones(ref_luma.shape, dtype=np.float32)

    for idx, f_u8 in enumerate(frames_u8):
        if idx == ref_idx:
            continue
        dy, dx = align_shift_fft(ref_u8, f_u8)
        aligned_u8 = shift_image(f_u8, dy, dx)
        mov_srgb = aligned_u8.astype(np.float32) * inv255
        # Smoothed per-pixel difference to reject moving objects without reacting to single-pixel noise
        diff = np.mean(np.abs(mov_srgb - ref_srgb), axis=2)
        diff_sm = _box_blur_1d(_box_blur_1d(diff, 2, 0), 2, 1)
        # Smooth Gaussian/Huber temporal weight
        w = np.exp(-0.5 * (diff_sm * diff_sm) * inv_tau2).astype(np.float32)
        w = np.where(w < 0.08, 0.0, w)

        mov_lin = np.power(mov_srgb, 2.2)
        accum += mov_lin * w[:, :, None]
        wsum += w

    return accum / wsum[:, :, None]


# ---------------------------------------------------------------------------
# Stage 1B: Multi-Exposure Bracketing HDR (Laplacian Pyramid Mertens Fusion)
# ---------------------------------------------------------------------------

def merge_hdr_mertens_srgb(frames_u8: list) -> np.ndarray:
    """Fuse aligned multi-exposure [EV-, EV0, EV+] frames via 4-level Mertens Laplacian pyramid."""
    if len(frames_u8) == 1:
        return frames_u8[0].astype(np.float32) * (1.0 / 255.0)

    # Use middle (EV0) frame as geometric anchor
    ref_idx = len(frames_u8) // 2
    ref_u8 = frames_u8[ref_idx]
    inv255 = np.float32(1.0 / 255.0)

    imgs = []
    for idx, f_u8 in enumerate(frames_u8):
        if idx != ref_idx:
            dy, dx = align_shift_fft(ref_u8, f_u8)
            f_u8 = shift_image(f_u8, dy, dx)
        imgs.append(f_u8.astype(np.float32) * inv255)

    # Sort brackets darkest -> brightest so EV- highlight rescue and EV+ shadow lift are always ordered
    imgs.sort(key=lambda im: float(im[::16, ::16].mean()))
    ref_idx = len(imgs) // 2

    # Check actual luminance spread across captured hardware brackets.
    # If the brightest bracket saturated at the sensor's max shutter+gain (e.g. dim room),
    # synthesize a virtual +1.2 EV shadow-lifted bracket in linear light so Mertens fusion
    # always recovers deep shadows cleanly.
    if len(imgs) >= 3:
        m_mid = float(imgs[ref_idx][::8, ::8].mean())
        m_hi = float(imgs[-1][::8, ::8].mean())
        if m_mid > 0.02 and m_hi < m_mid * 1.18:
            lin_hi = np.power(np.clip(imgs[-1], 0.0, 1.0), 2.2)
            # Smooth highlight-safe +1.2 EV gain (2.3x in shadows, rolls off before clipping)
            boosted = np.clip((lin_hi * 2.3) / (1.0 + 0.45 * lin_hi), 0.0, 1.0)
            imgs[-1] = np.power(boosted, 1.0 / 2.2).astype(np.float32)

    weights = []
    # Highlight clipping map of the EV0 reference frame: wherever EV0 is near/at clipping
    # (e.g. bright monitor/phone screen in a dark room), heavily suppress clipped EV0/EV+
    # frames and pull detail cleanly from the EV- dark bracket.
    ref_peak = np.max(imgs[ref_idx], axis=2) if len(imgs) >= 3 else None

    for idx, im in enumerate(imgs):
        # Mertens quality metrics: Contrast * Saturation * Well-Exposedness
        y = 0.299 * im[:, :, 0] + 0.587 * im[:, :, 1] + 0.114 * im[:, :, 2]
        # Contrast (Laplacian magnitude)
        yp = np.pad(y, 1, mode="edge")
        lap = np.abs(4.0 * y - yp[:-2, 1:-1] - yp[2:, 1:-1] - yp[1:-1, :-2] - yp[1:-1, 2:])
        # Saturation (RGB standard deviation)
        mu = (im[:, :, 0] + im[:, :, 1] + im[:, :, 2]) * (1.0 / 3.0)
        sat = np.sqrt(
            ((im[:, :, 0] - mu) ** 2 + (im[:, :, 1] - mu) ** 2 + (im[:, :, 2] - mu) ** 2) * (1.0 / 3.0)
            + 1e-6
        )
        # Well-exposedness (Gaussian around 0.48, tighter sigma=0.20 rejects clipped 0.95+ hard)
        d0 = im[:, :, 0] - 0.48
        d1 = im[:, :, 1] - 0.48
        d2 = im[:, :, 2] - 0.48
        exp_w = np.exp(-0.5 * (d0 * d0 + d1 * d1 + d2 * d2) / (0.20 * 0.20))

        # Strongly penalize individually clipped pixels (>0.94) so screens never blow out
        pk = np.max(im, axis=2)
        clip_pen = np.clip((0.99 - pk) * 14.0, 0.01, 1.0)
        w = (lap + 0.025) * (sat + 0.06) * (exp_w + 1e-3) * clip_pen

        if ref_peak is not None:
            hl_mask = np.clip((ref_peak - 0.80) * 6.0, 0.0, 1.0)
            if idx == 0:
                # Boost EV- dark bracket strongly on clipped highlights
                w = w * (1.0 + 12.0 * hl_mask)
            elif idx == len(imgs) - 1:
                # Suppress EV+ bright bracket on already-bright areas
                w = w * (1.0 - 0.92 * hl_mask)
        weights.append(w.astype(np.float32))

    # Normalize weights across exposures
    w_tot = np.zeros_like(weights[0])
    for w in weights:
        w_tot += w
    w_tot = np.maximum(w_tot, 1e-6)
    weights = [w / w_tot for w in weights]

    # 4-level Gaussian/Laplacian Pyramid blend (eliminates seam halos!)
    levels = 4
    n_exp = len(imgs)
    g_imgs = [[im] for im in imgs]
    g_wts = [[w] for w in weights]
    for _ in range(levels - 1):
        for k in range(n_exp):
            g_imgs[k].append(_downsample_2x(g_imgs[k][-1]))
            g_wts[k].append(_downsample_2x(g_wts[k][-1]))

    # Blend coarsest level
    fused = np.zeros_like(g_imgs[0][-1])
    for k in range(n_exp):
        fused += g_imgs[k][-1] * g_wts[k][-1][:, :, None]

    # Reconstruct fine-to-coarse Laplacian levels
    for lv in range(levels - 2, -1, -1):
        th, tw = g_imgs[0][lv].shape[:2]
        fused = _upsample_to(fused, th, tw)
        for k in range(n_exp):
            up_k = _upsample_to(g_imgs[k][lv + 1], th, tw)
            lap_k = g_imgs[k][lv] - up_k
            fused += lap_k * g_wts[k][lv][:, :, None]

    return np.clip(fused, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Stage 2: Stock 17x13 Lens Shading Correction (LSC) in Linear Light
# ---------------------------------------------------------------------------

def _transform_mesh(mesh_2d: np.ndarray, rot_deg: int, mirror: bool) -> np.ndarray:
    """Apply the exact same clockwise rotation + horizontal flip as GStreamer videoflip."""
    k_ccw = (-int(rot_deg // 90)) % 4  # np.rot90 rotates CCW
    out = np.rot90(mesh_2d, k=k_ccw)
    if mirror:
        out = np.fliplr(out)
    return out


def apply_lsc_linear(lin_rgb: np.ndarray, profile: dict, rot_deg: int, mirror: bool) -> np.ndarray:
    if not profile or "lsc_r" not in profile:
        return lin_rgb
    h, w, _ = lin_rgb.shape
    lw = int(profile.get("lsc_w", 17))
    lh = int(profile.get("lsc_h", 13))
    base_strength = float(profile.get("lsc_strength", 0.60))

    # Adapt LSC strength slightly downward in very dark scenes to avoid boosting corner shadow noise
    mean_lin = float(np.mean(lin_rgb))
    if mean_lin < 0.04:
        strength = base_strength * 0.65
    elif mean_lin < 0.10:
        strength = base_strength * 0.82
    else:
        strength = base_strength

    # Build transformed 17x13 -> (h, w) RGB gain map
    # Blend R/B meshes 50% toward G luminance mesh so optical vignetting is fixed cleanly
    # without introducing global red/magenta tint (e.g. IMX355 golden R/G=1.10).
    mg_raw = _transform_mesh(np.asarray(profile["lsc_g"], dtype=np.float32).reshape((lh, lw)), rot_deg, mirror)
    gains = []
    for key in ("lsc_r", "lsc_g", "lsc_b"):
        m = _transform_mesh(np.asarray(profile[key], dtype=np.float32).reshape((lh, lw)), rot_deg, mirror)
        if key != "lsc_g":
            m = 0.50 * m + 0.50 * mg_raw
        # Dampen corner boost: gain_eff = 1 + strength * (gain - 1), capped cleanly at 1.95x
        m_eff = np.clip(1.0 + strength * (m - 1.0), 0.92, 1.95)
        gains.append(_resize_float_plane(m_eff, w, h))
    gain_map = np.stack(gains, axis=2)

    # Strict highlight-preserving knee: fade LSC completely to 1.0 above 0.45 linear so bright
    # screens and white light sources near corners NEVER get pushed into clipping.
    peak_c = np.max(lin_rgb, axis=2, keepdims=True)
    hl_protect = np.clip((0.72 - peak_c) / 0.35, 0.0, 1.0)
    eff_gain = 1.0 + (gain_map - 1.0) * hl_protect
    return np.clip(lin_rgb * eff_gain, 0.0, 1.15)


# ---------------------------------------------------------------------------
# Stage 2B: Highlight-Safe Neutral White-Balance / Green-Cast Guard
# ---------------------------------------------------------------------------

def apply_awb_green_guard(lin_rgb: np.ndarray) -> np.ndarray:
    """Correct residual green cast caused when bright white lamps fool Gray-World AWB.

    Measures only non-clipped, non-black neutral-ish midtones so vivid foliage/objects
    are never over-corrected, while white light sources & skin stay clean and neutral.
    """
    sub = lin_rgb[::8, ::8, :]
    r, g, b = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    chroma = (mx - mn) / (mx + 1e-4)

    # Select midtone, near-neutral pixels (exclude clipped highlights and black shadows)
    mask = (y > 0.04) & (y < 0.68) & (chroma < 0.38)
    if int(np.count_nonzero(mask)) < 80:
        return lin_rgb

    rm = float(np.mean(r[mask]))
    gm = float(np.mean(g[mask]))
    bm = float(np.mean(b[mask]))
    rb_avg = 0.5 * (rm + bm)
    if rb_avg <= 1e-4 or gm <= 1e-4:
        return lin_rgb

    # Neutralize genuine green excess (>5% over R/B mean) or red excess (>7% over G/B mean)
    out = lin_rgb
    g_ratio = gm / rb_avg
    r_ratio = rm / max(1e-4, 0.5 * (gm + bm))
    if g_ratio > 1.05:
        corr_g = max(0.86, min(1.0, 1.04 / g_ratio))
        out = out.copy()
        out[:, :, 1] *= corr_g
    elif r_ratio > 1.07:
        corr_r = max(0.88, min(1.0, 1.05 / r_ratio))
        out = out.copy()
        out[:, :, 0] *= corr_r
    return out


# ---------------------------------------------------------------------------
# Stage 3: Stock-Inspired Local Tone Mapping (LTM) + Filmic S-Curve + Vibrance
# ---------------------------------------------------------------------------

def apply_ltm_and_filmic(lin_rgb: np.ndarray, profile: dict, is_hdr: bool = False) -> np.ndarray:
    ltm_str = float(profile.get("ltm_strength", 0.32)) if profile else 0.32
    vibrance = float(profile.get("vibrance", 0.12)) if profile else 0.12

    # Compute linear luma and smooth low-frequency illumination envelope (8x downsampled)
    y = np.maximum(1e-4, 0.2126 * lin_rgb[:, :, 0] + 0.7152 * lin_rgb[:, :, 1] + 0.0722 * lin_rgb[:, :, 2])
    h, w = y.shape
    log_y = np.log2(y + 0.005)
    # Fast coarse envelope (downsample 8x, Gaussian blur, upsample)
    ds = _downsample_2x(_downsample_2x(_downsample_2x(log_y)))
    env_ds = _fast_gaussian_approx(ds, 6)
    env = _resize_float_plane(env_ds, w, h)

    # Local Tone Mapping: lift dark regions (env < -1.8), compress highlights (env > -0.8)
    # CRITICAL: gate positive shadow lift by per-pixel luminance (1 - smoothstep(0.18..0.45))
    # so a bright screen surrounded by a dark room NEVER inherits the dark room's +EV lift!
    target_mid = -1.75
    delta = (target_mid - env) * ltm_str
    max_lift = 0.25 if is_hdr else 0.70
    delta = np.clip(delta, -0.55, max_lift)
    pixel_shadow_gate = np.clip((0.45 - y) / 0.27, 0.0, 1.0)
    delta = np.where(delta > 0.0, delta * pixel_shadow_gate, delta)
    gain_ltm = np.power(2.0, delta).astype(np.float32)
    mapped = lin_rgb * gain_ltm[:, :, None]

    # Highlight-preserving soft-knee shoulder (leaves 0..0.72 linear 1:1 so clean whites stay
    # bright and punchy instead of dull gray, while smoothly rolling off speculars >0.72 -> 1.0)
    knee = 0.70 if is_hdr else 0.74
    over = np.maximum(0.0, mapped - knee)
    headroom = 1.0 - knee
    mapped = np.where(
        mapped <= knee,
        mapped,
        knee + headroom * (over / (over + headroom * 0.85)),
    )
    mapped = np.clip(mapped, 0.0, 1.0)

    # Linear -> sRGB gamma
    srgb = np.power(mapped, 1.0 / 2.2)

    # Subtle Filmic S-curve contrast in sRGB (crisp blacks + clean bright midtones)
    srgb = np.clip(srgb + 0.36 * srgb * (1.0 - srgb) * (srgb - 0.44), 0.0, 1.0)

    # Smart Vibrance (boosts muted colors more than already-vivid skin/reds)
    eff_vib = vibrance + 0.06
    if eff_vib > 0.0:
        mx = np.max(srgb, axis=2, keepdims=True)
        mn = np.min(srgb, axis=2, keepdims=True)
        sat = (mx - mn) / (mx + 1e-4)
        luma = (0.299 * srgb[:, :, 0:1] + 0.587 * srgb[:, :, 1:2] + 0.114 * srgb[:, :, 2:3])
        boost = 1.0 + eff_vib * (1.0 - sat) * np.clip(luma * 2.5, 0.0, 1.0)
        srgb = np.clip(luma + (srgb - luma) * boost, 0.0, 1.0)

    return srgb


# ---------------------------------------------------------------------------
# Stage 4: YCbCr Edge-Preserving Chroma Denoise + Halo-Free Multi-Scale ASF
# ---------------------------------------------------------------------------

def apply_denoise_and_asf(srgb: np.ndarray, profile: dict, is_night: bool = False) -> np.ndarray:
    sharpen = float(profile.get("sharpen", 0.42)) if profile else 0.42
    if is_night:
        sharpen *= 0.80  # gentler sharpening in night mode

    r, g, b = srgb[:, :, 0], srgb[:, :, 1], srgb[:, :, 2]
    # BT.709 / sRGB YCbCr
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b

    # 1. Edge-preserving Chroma Denoise (eliminates color blotches in shadows!)
    yp = np.pad(y, 1, mode="edge")
    grad = np.abs(yp[1:-1, 2:] - yp[1:-1, :-2]) + np.abs(yp[2:, 1:-1] - yp[:-2, 1:-1])
    edge_mask = np.clip(grad * 9.0, 0.0, 1.0)  # 0 on flat noisy areas, 1 on crisp edges
    c_rad = 4 if is_night else 3
    cb_sm = _fast_gaussian_approx(cb, c_rad)
    cr_sm = _fast_gaussian_approx(cr, c_rad)
    # Apply stronger chroma smoothing in dark regions where chroma noise lives
    dark_w = np.clip(1.15 - y * 0.9, 0.35, 1.0)
    blend = (1.0 - edge_mask) * dark_w
    cb = cb * (1.0 - blend) + cb_sm * blend
    cr = cr * (1.0 - blend) + cr_sm * blend
    # Gently desaturate ultra-dark shadow floor (y < 0.10) to keep blacks clean & neutral
    shadow_sat = np.clip((y - 0.015) / 0.095, 0.35, 1.0)
    cb *= shadow_sat
    cr *= shadow_sat

    # 2. Multi-Scale Halo-Free Sharpening (Qualcomm ASF352 style with noise coring + clamp)
    y_fine = _box_blur_1d(_box_blur_1d(y, 1, 0), 1, 1)
    y_med = _box_blur_1d(_box_blur_1d(y, 3, 0), 3, 1)
    det_fine = y - y_fine
    det_med = y_fine - y_med

    # Adaptive noise coring (dead-zone rejects sensor grain in flat shadows)
    core_thr = 0.016 if is_night else 0.010
    det_fine_c = np.sign(det_fine) * np.maximum(0.0, np.abs(det_fine) - core_thr)
    det_med_c = np.sign(det_med) * np.maximum(0.0, np.abs(det_med) - core_thr * 0.7)

    # Overshoot clamp prevents harsh white halos on high-contrast silhouettes
    detail = np.clip((sharpen * 1.18) * det_fine_c + (sharpen * 0.52) * det_med_c, -0.095, 0.095)
    y = np.clip(y + detail, 0.0, 1.0)

    # YCbCr -> sRGB
    ro = np.clip(y + 1.402 * cr, 0.0, 1.0)
    go = np.clip(y - 0.344136 * cb - 0.714136 * cr, 0.0, 1.0)
    bo = np.clip(y + 1.772 * cb, 0.0, 1.0)
    return np.stack([ro, go, bo], axis=2)


# ---------------------------------------------------------------------------
# Stage 5: Real Smartphone EXIF Builder + Top-Level Develop Entrypoint
# ---------------------------------------------------------------------------

def build_exif_bytes(profile: dict, exp_sec: float = None, iso: int = None, focus_dist_m: float = None, mode_tag: str = "MFNR"):
    exif = Image.Exif()
    now_str = datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S")
    # IFD0 standard tags
    exif[271] = "realme"                                  # Make
    exif[272] = "RMX5010"                                 # Model (Realme GT7 Pro)
    exif[305] = f"Pista Mainline Linux ISP ({mode_tag})"  # Software
    exif[306] = now_str                                   # DateTime

    # ExifIFD (0x8769) photography tags
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd[36867] = now_str                             # DateTimeOriginal
    exif_ifd[36868] = now_str                             # DateTimeDigitized

    if profile:
        if "lens_model" in profile:
            exif_ifd[42036] = str(profile["lens_model"])  # LensModel
        if "focal_mm" in profile:
            exif_ifd[37386] = (int(round(profile["focal_mm"] * 100)), 100)  # FocalLength
        if "focal_35mm" in profile:
            exif_ifd[41989] = int(profile["focal_35mm"])  # FocalLengthIn35mmFilm
        if "f_number" in profile:
            exif_ifd[33437] = (int(round(profile["f_number"] * 100)), 100)  # FNumber

    if exp_sec and exp_sec > 0:
        if exp_sec < 1.0:
            exif_ifd[33434] = (1, max(1, int(round(1.0 / exp_sec))))        # ExposureTime
        else:
            exif_ifd[33434] = (int(round(exp_sec * 100)), 100)
    if iso and iso > 0:
        exif_ifd[34855] = int(iso)                        # ISOSpeedRatings
    if focus_dist_m and focus_dist_m > 0:
        exif_ifd[37382] = (int(round(focus_dist_m * 100)), 100)             # SubjectDistance

    return exif.tobytes()


def develop_photo(
    frames_u8: list,
    mode: str,
    profile: dict,
    rot_deg: int,
    mirror: bool,
    exp_sec: float = None,
    iso: int = None,
    focus_dist_m: float = None,
):
    """Run the full computational photography pipeline and return (PIL.Image, exif_bytes, stats)."""
    t0 = time.monotonic()

    if mode == "raw":
        out_img = Image.fromarray(frames_u8[-1], mode="RGB")
        exif_b = build_exif_bytes(profile, exp_sec, iso, focus_dist_m, mode_tag="RAW-Preview")
        return out_img, exif_b, {"ms": (time.monotonic() - t0) * 1000.0, "n": 1}

    if mode == "hdr":
        srgb = merge_hdr_mertens_srgb(frames_u8)
        lin = np.power(srgb, 2.2)
        lin = apply_lsc_linear(lin, profile, rot_deg, mirror)
        lin = apply_awb_green_guard(lin)
        # Lighter highlight-safe LTM in HDR mode since Mertens already fused dynamic range
        hdr_prof = dict(profile or {})
        hdr_prof["ltm_strength"] = float(hdr_prof.get("ltm_strength", 0.35)) * 0.35
        srgb = apply_ltm_and_filmic(lin, hdr_prof, is_hdr=True)
        srgb = apply_denoise_and_asf(srgb, hdr_prof, is_night=False)
        tag = f"HDR-{len(frames_u8)}f"
    else:
        is_night = (mode == "night")
        lin = merge_mfnr_linear(frames_u8)
        lin = apply_lsc_linear(lin, profile, rot_deg, mirror)
        lin = apply_awb_green_guard(lin)
        srgb = apply_ltm_and_filmic(lin, profile, is_hdr=False)
        srgb = apply_denoise_and_asf(srgb, profile, is_night=is_night)
        tag = f"{'Night' if is_night else 'Std'}-MFNR-{len(frames_u8)}f"

    out_u8 = np.clip(srgb * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    out_img = Image.fromarray(out_u8, mode="RGB")
    exif_b = build_exif_bytes(profile, exp_sec, iso, focus_dist_m, mode_tag=tag)
    return out_img, exif_b, {"ms": (time.monotonic() - t0) * 1000.0, "n": len(frames_u8)}
