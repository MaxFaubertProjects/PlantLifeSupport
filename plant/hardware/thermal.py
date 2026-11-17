"""USB thermal camera (AccuMEMS SK00) — leaf/scene temperature map.

The SK00 is a shutterless 96x96 bolometer that enumerates as a plain UVC
camera (USB 38be:0184) streaming 100x126 "YUYV" frames at 25 fps. The frame
is not video: every 2-byte pixel is a little-endian uint16 raw count.
Rows 0-99 are the image, the rest is sensor data / telemetry (see
``tools/sk00_decode.py`` for the reverse-engineering notes).

Each pixel has its own large, stable offset (several °C worth of counts), so
the raw image is unreadable on its own. A one-off **flat-field** reference —
the lens looking at something of uniform temperature for a second — removes
it. After that, temperature is linear in counts:

    T = T_ref + (counts - flat - drift) / counts_per_celsius

where ``T_ref`` is the air temperature (DS18B20) when the reference was taken.
The reference is saved to ``data/thermal_flat.npz`` and survives restarts.

Drift: the whole image's zero point moves as the sensor itself warms up
(measured on the Pi: ~200 counts ≈ 4.5 °C in the first 10 minutes after
power-up, then nearly flat). It tracks telemetry word ``frame[120][5]``
almost linearly, at about -0.63 counts per unit, so

    drift = drift_counts_per_tel * (tel_now - tel_at_calibration)

Calibrate once the camera has been powered for ~15 minutes for best results.

Frames are read with ``v4l2-ctl`` (already on Raspberry Pi OS), so this needs
only numpy — no OpenCV. The PNG encoder is stdlib (zlib) for the same reason.

Config (all optional, under ``thermal:`` in config.yaml):
    enabled: auto            # auto | true | false
    device: auto             # auto = find the 38be:0184 UVC node, or /dev/videoN
    counts_per_celsius: 44   # measured on a hand at 22 °C room (HARDWARE_V2.md)
    frames: 8                # frames averaged per reading (noise ~1/sqrt(n))
    flat_path: data/thermal_flat.npz
    drift_counts_per_tel: -0.63   # 0 disables drift compensation
    rotation: 90             # default image rotation, degrees clockwise

The rotation can also be changed from the dashboard (Rotate button); that
choice is saved in ``thermal_settings.json`` next to the flat-field file and
overrides the config default.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

W, H_TOTAL, H_IMG = 100, 126, 100
FRAME_BYTES = W * H_TOTAL * 2
# Telemetry word that tracks the sensor's own temperature (see "Drift" above).
_TEL_ROW, _TEL_COL = 120, 5
USB_VENDOR, USB_PRODUCT = "38be", "0184"

# Frames thrown away at the start of every stream: the first one or two
# after opening the device are sometimes partial.
_WARMUP_FRAMES = 2
# A reading is reused for this long so the dashboard's image and numbers
# (and several open browser tabs) share one capture.
_CACHE_SECONDS = 3.0
# Never stretch the colour scale over less than this, or pure sensor noise
# gets painted as a full rainbow.
_MIN_SPAN_C = 2.0


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class ThermalReading:
    """One processed frame, ready for the API."""
    ts: float
    calibrated: bool
    png: bytes                       # 100x100 false-colour PNG
    stats: dict[str, Any] = field(default_factory=dict)

    def to_api(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "calibrated": self.calibrated,
            "image": "data:image/png;base64," + base64.b64encode(self.png).decode(),
            **self.stats,
        }


# ── Small numeric helpers (numpy imported lazily so the Mac CLI stays light) ──

def _np():
    import numpy as np  # noqa: WPS433 — deliberate lazy import
    return np


# "Ironbow"-style palette: black → purple → red → orange → yellow → white.
_PALETTE_STOPS = [
    (0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99),
    (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 255, 164),
]


def colorize(norm):
    """Map an array of 0..1 values to uint8 RGB with the ironbow palette."""
    np = _np()
    stops = np.array(_PALETTE_STOPS, dtype=float)
    x = np.clip(norm, 0.0, 1.0) * (len(stops) - 1)
    i = np.clip(x.astype(int), 0, len(stops) - 2)
    f = (x - i)[..., None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).round().astype("uint8")


def encode_png(rgb) -> bytes:
    """Encode an HxWx3 uint8 array as PNG using only zlib."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def rotate_cw(a, degrees: int):
    """Rotate a 2-D array clockwise by a multiple of 90 degrees."""
    return _np().rot90(a, k=-(int(degrees) // 90) % 4)


def summarize(temps, air_c: Optional[float]) -> dict[str, Any]:
    """Temperature statistics + colour range for a calibrated frame (°C)."""
    np = _np()
    cy, cx = H_IMG // 2, W // 2
    spot = float(temps[cy - 3:cy + 4, cx - 3:cx + 4].mean())
    hot = np.unravel_index(int(temps.argmax()), temps.shape)
    cold = np.unravel_index(int(temps.argmin()), temps.shape)
    lo, hi = (float(v) for v in np.percentile(temps, [1, 99]))
    if hi - lo < _MIN_SPAN_C:
        mid = (hi + lo) / 2
        lo, hi = mid - _MIN_SPAN_C / 2, mid + _MIN_SPAN_C / 2
    r = lambda v: round(float(v), 1)  # noqa: E731
    return {
        "spot_c": r(spot),
        "min_c": r(temps.min()),
        "max_c": r(temps.max()),
        "mean_c": r(temps.mean()),
        "hot_xy": [int(hot[1]), int(hot[0])],
        "cold_xy": [int(cold[1]), int(cold[0])],
        "scale_lo_c": r(lo),
        "scale_hi_c": r(hi),
        "air_c": r(air_c) if air_c is not None else None,
        "spot_minus_air_c": r(spot - air_c) if air_c is not None else None,
    }


# ── Device discovery ──────────────────────────────────────────────────────────

def find_device() -> Optional[str]:
    """Return /dev/videoN for the SK00's capture node, or None.

    Walks /sys/class/video4linux and matches the USB vendor/product ids of
    the parent device. A UVC camera exposes two nodes (capture + metadata);
    the capture one has ``index`` 0.
    """
    base = Path("/sys/class/video4linux")
    if not base.exists():
        return None
    for node in sorted(base.glob("video*"), key=lambda p: int(p.name[5:] or 0)):
        try:
            if (node / "index").read_text().strip() != "0":
                continue
            dev = (node / "device").resolve()
        except OSError:
            continue
        # Interface dir → its parent USB device carries idVendor/idProduct.
        for parent in (dev, *dev.parents):
            vid, pid = parent / "idVendor", parent / "idProduct"
            if vid.exists() and pid.exists():
                if (vid.read_text().strip() == USB_VENDOR
                        and pid.read_text().strip() == USB_PRODUCT):
                    return f"/dev/{node.name}"
                break
    return None


# ── Real camera ───────────────────────────────────────────────────────────────

class ThermalCamera:
    """SK00 thermal camera driver with flat-field calibration.

    Thread-safe; every public method may block for ~1 s while frames stream.
    ``air_temp`` is a callable returning the current air temperature in °C
    (or None) — used as the calibration reference and for leaf−air ΔT.
    """

    def __init__(self, cfg: dict | None = None,
                 air_temp: Callable[[], Optional[float]] | None = None) -> None:
        tcfg = (cfg or {}).get("thermal", {}) or {}
        self._device_cfg = str(tcfg.get("device", "auto"))
        self._cpc = float(tcfg.get("counts_per_celsius", 44.0))
        self._frames = max(1, int(tcfg.get("frames", 8)))
        self._flat_path = Path(tcfg.get("flat_path", "data/thermal_flat.npz"))
        self._drift_k = float(tcfg.get("drift_counts_per_tel", -0.63))
        self._settings_path = self._flat_path.with_name("thermal_settings.json")
        self._rotation = int(tcfg.get("rotation", 90)) % 360
        try:
            saved = json.loads(self._settings_path.read_text())
            self._rotation = int(saved.get("rotation", self._rotation)) % 360
        except (OSError, ValueError):
            pass
        self._air_temp = air_temp or (lambda: None)
        self._lock = threading.Lock()
        self._cache: Optional[ThermalReading] = None
        self._flat = None          # np.ndarray 100x100 float
        self._flat_ref_c: Optional[float] = None
        self._flat_ts: Optional[float] = None
        self._flat_ref_assumed = False
        self._flat_tel: Optional[float] = None
        self._load_flat()

    # -- state ---------------------------------------------------------------

    @property
    def device(self) -> Optional[str]:
        if self._device_cfg != "auto":
            return self._device_cfg if Path(self._device_cfg).exists() else None
        return find_device()

    @property
    def available(self) -> bool:
        return self.device is not None

    def info(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "device": self.device,
            "calibrated": self._flat is not None,
            "calibrated_at": self._flat_ts,
            "calibration_ref_c": self._flat_ref_c,
            "calibration_ref_assumed": self._flat_ref_assumed,
            "rotation": self._rotation,
        }

    def rotate(self) -> dict[str, Any]:
        """Turn the displayed image a further 90° clockwise and remember it."""
        with self._lock:
            self._rotation = (self._rotation + 90) % 360
            try:
                self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                self._settings_path.write_text(json.dumps({"rotation": self._rotation}))
            except OSError as exc:
                log.warning("Thermal: could not save rotation — %s", exc)
            self._cache = None
        return self.info()

    def _load_flat(self) -> None:
        if not self._flat_path.exists():
            return
        try:
            np = _np()
            with np.load(self._flat_path) as z:
                self._flat = z["flat"].astype(float)
                self._flat_ref_c = float(z["ref_c"])
                self._flat_ts = float(z["ts"])
                self._flat_ref_assumed = bool(z["ref_assumed"]) if "ref_assumed" in z else False
                self._flat_tel = float(z["tel"]) if "tel" in z else None
            log.info("Thermal: loaded flat-field reference (%.1f °C, %s)",
                     self._flat_ref_c, time.strftime("%Y-%m-%d %H:%M", time.localtime(self._flat_ts)))
        except Exception as exc:  # corrupt / old format → just recalibrate
            log.warning("Thermal: could not load %s — %s", self._flat_path, exc)
            self._flat = None

    # -- capture -------------------------------------------------------------

    def _grab(self, n: int):
        """Stream n frames; return (mean 100x100 image in counts, telemetry)."""
        np = _np()
        dev = self.device
        if dev is None:
            raise RuntimeError("thermal camera not connected")
        count = n + _WARMUP_FRAMES
        proc = subprocess.run(
            ["v4l2-ctl", "-d", dev,
             "--set-fmt-video=width=100,height=126,pixelformat=YUYV",
             "--stream-mmap", f"--stream-count={count}", "--stream-to=-"],
            capture_output=True, timeout=15,
        )
        got = len(proc.stdout) // FRAME_BYTES
        if got <= _WARMUP_FRAMES:
            err = proc.stderr.decode(errors="replace").strip()[-200:]
            raise RuntimeError(f"thermal camera returned no frames ({err or 'empty stream'})")
        frames = np.frombuffer(proc.stdout[: got * FRAME_BYTES], dtype="<u2")
        frames = frames.reshape(got, H_TOTAL, W)[_WARMUP_FRAMES:]
        tel = float(np.median(frames[:, _TEL_ROW, _TEL_COL]))
        return frames[:, :H_IMG, :].astype(float).mean(axis=0), tel

    # -- public --------------------------------------------------------------

    def calibrate(self) -> dict[str, Any]:
        """Capture the flat-field reference. The lens must be looking at
        something of uniform temperature (a blank wall, a piece of card held
        just in front of it) while this runs (~2 s)."""
        np = _np()
        with self._lock:
            flat, tel = self._grab(max(25, self._frames * 3))
            air = self._safe_air()
            assumed = air is None
            ref = air if air is not None else 22.0
            ts = time.time()
            self._flat_path.parent.mkdir(parents=True, exist_ok=True)
            # np.savez appends .npz unless the name already ends with it.
            np.savez(self._flat_path, flat=flat, ref_c=ref, ts=ts,
                     ref_assumed=assumed, tel=tel)
            self._flat, self._flat_ref_c, self._flat_ts = flat, ref, ts
            self._flat_ref_assumed, self._flat_tel = assumed, tel
            self._cache = None
            log.info("Thermal: calibrated at %.1f °C%s", ref, " (assumed)" if assumed else "")
        return self.info()

    def read(self, max_age: float = _CACHE_SECONDS) -> ThermalReading:
        """Return a fresh-enough processed frame (cached for ``max_age`` s)."""
        with self._lock:
            if self._cache and time.time() - self._cache.ts < max_age:
                return self._cache
            raw, tel = self._grab(self._frames)
            air = self._safe_air()
            self._cache = self._process(raw, air, tel)
            return self._cache

    # -- internals -----------------------------------------------------------

    def _safe_air(self) -> Optional[float]:
        try:
            return self._air_temp()
        except Exception as exc:
            log.debug("Thermal: air temperature unavailable — %s", exc)
            return None

    def _process(self, raw, air: Optional[float], tel: Optional[float] = None) -> ThermalReading:
        np = _np()
        if self._flat is None:
            # Uncalibrated: remove the worst column striping so the user at
            # least sees the camera is alive, but report no temperatures.
            img = rotate_cw(raw - np.median(raw, axis=0)[None, :], self._rotation)
            lo, hi = np.percentile(img, [1, 99])
            png = encode_png(colorize((img - lo) / ((hi - lo) or 1)))
            return ThermalReading(ts=time.time(), calibrated=False, png=png,
                                  stats={"air_c": round(air, 1) if air is not None else None,
                                         "rotation": self._rotation})
        drift = 0.0
        if self._drift_k and tel is not None and self._flat_tel is not None:
            drift = self._drift_k * (tel - self._flat_tel)
        temps = self._flat_ref_c + (raw - self._flat - drift) / self._cpc
        temps = rotate_cw(temps, self._rotation)
        stats = summarize(temps, air)
        stats["drift_c"] = round(drift / self._cpc, 2)
        stats.update({
            "calibrated_at": self._flat_ts,
            "calibration_ref_c": self._flat_ref_c,
            "calibration_ref_assumed": self._flat_ref_assumed,
            "rotation": self._rotation,
        })
        lo, hi = stats["scale_lo_c"], stats["scale_hi_c"]
        png = encode_png(colorize((temps - lo) / (hi - lo)))
        return ThermalReading(ts=time.time(), calibrated=True, png=png, stats=stats)


# ── Simulation stand-in ───────────────────────────────────────────────────────

class MockThermalCamera:
    """Synthetic thermal view (a cool leafy blob on a warm background) so the
    dashboard can be developed without the camera."""

    available = True

    def __init__(self, cfg: dict | None = None,
                 air_temp: Callable[[], Optional[float]] | None = None) -> None:
        self._air_temp = air_temp or (lambda: 22.0)
        self._calibrated_at = time.time()
        self._rotation = 0

    def rotate(self) -> dict[str, Any]:
        self._rotation = (self._rotation + 90) % 360
        return self.info()

    def info(self) -> dict[str, Any]:
        return {"available": True, "device": "mock", "calibrated": True,
                "calibrated_at": self._calibrated_at, "calibration_ref_c": 22.0,
                "calibration_ref_assumed": False, "rotation": self._rotation}

    def calibrate(self) -> dict[str, Any]:
        self._calibrated_at = time.time()
        return self.info()

    def read(self, max_age: float = _CACHE_SECONDS) -> ThermalReading:
        np = _np()
        air = self._air_temp() or 22.0
        yy, xx = np.mgrid[0:H_IMG, 0:W]
        t = time.time()
        blob = np.exp(-(((xx - 50) / 22) ** 2 + ((yy - 58 + 2 * np.sin(t / 7)) / 28) ** 2))
        temps = air + 0.6 - 2.2 * blob + 0.4 * (yy / H_IMG) + np.random.normal(0, 0.05, blob.shape)
        temps = rotate_cw(temps, self._rotation)
        stats = summarize(temps, air)
        stats.update({"calibrated_at": self._calibrated_at, "calibration_ref_c": 22.0,
                      "calibration_ref_assumed": False})
        png = encode_png(colorize((temps - stats["scale_lo_c"])
                                  / (stats["scale_hi_c"] - stats["scale_lo_c"])))
        return ThermalReading(ts=t, calibrated=True, png=png, stats=stats)
