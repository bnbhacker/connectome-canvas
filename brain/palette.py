"""
Colour is not a fly decision — a fly has no notion of paint. It is read off
the shape of the activity: which part of the nervous system carried the
window's spikes, and how loud the window was compared with the ones before.

  hue         the balance between central brain and optic lobe in this window's
              spikes, swept across the wheel (optic-heavy → teal, balanced →
              violet, central-heavy → amber/red), with the VNC share pushing
              toward gold
  saturation  the Kenyon-cell (mushroom body) share of spikes
  lightness   this window's rate against the running median — a network-wide
              burst leaves a near-white mark, a quiet window a dim one
  width       forward drive (DNa01) — handed in by the painter

That mapping is a choice a person made, it is fixed, and the site says so.
"""
from __future__ import annotations

import colorsys
from collections import deque
from dataclasses import dataclass

import numpy as np

from .sim import Brain


@dataclass
class Colour:
    rgb: tuple[int, int, int]
    hex: str
    hue: float
    sat: float
    light: float
    f_optic: float
    f_central: float
    f_vnc: float
    f_kc: float


class Palette:
    def __init__(self, brain: Brain):
        sc = np.char.lower(brain.superclass.astype(str))
        ty = brain.types.astype(str)
        is_optic = np.zeros(brain.n, dtype=bool)
        is_vnc = np.zeros(brain.n, dtype=bool)
        for key in ("optic", "visual", "ol_", "lamina", "medulla", "lobula"):
            is_optic |= np.char.find(sc, key) >= 0
        for key in ("vnc", "motor", "ascending", "leg", "wing", "haltere", "neck", "abdominal"):
            is_vnc |= np.char.find(sc, key) >= 0
        self.optic = is_optic & ~is_vnc
        self.vnc = is_vnc
        self.central = ~(self.optic | self.vnc)
        self.kc = np.fromiter((t.startswith("KC") for t in ty), dtype=bool, count=brain.n)
        self.history: deque = deque(maxlen=40)
        self.balance_hist: deque = deque(maxlen=60)

    def reset(self) -> None:
        self.history.clear()
        self.balance_hist.clear()

    def colour(self, counts: np.ndarray, ms: float) -> Colour:
        total = float(counts.sum())
        if total <= 0:
            return Colour((90, 90, 96), "#5a5a60", 0, 0, 0.35, 0, 0, 0, 0)
        fo = float(counts[self.optic].sum()) / total
        fc = float(counts[self.central].sum()) / total
        fv = float(counts[self.vnc].sum()) / total
        fk = float(counts[self.kc].sum()) / total

        # hue: how far this window's central/optic balance sits from its own recent
        # median, swept across the wheel — a window that leans more central than
        # usual goes violet → red → amber, one that leans more optic goes teal → blue
        balance = fc / (fo + fc + 1e-6)                 # 0 = all optic, 1 = all central
        self.balance_hist.append(balance)
        med_b = float(np.median(self.balance_hist)) if len(self.balance_hist) >= 5 else balance
        hue = (0.50 + np.clip(9.0 * (balance - med_b), -0.45, 0.45)) % 1.0
        hue = (hue * (1 - fv) + 0.11 * fv) % 1.0         # VNC pulls toward gold
        sat = float(np.clip(0.45 + 0.55 * min(1.0, fk * 10.0), 0, 1))

        # lightness: this window against the running median of recent windows
        self.history.append(total)
        med = float(np.median(self.history)) if len(self.history) >= 5 else total
        loud = np.log2(max(total, 1.0) / max(med, 1.0))   # 0 = typical, +1 = twice as loud
        light = float(np.clip(0.55 + 0.12 * loud, 0.3, 0.95))

        r, g, b = colorsys.hls_to_rgb(hue, light, sat)
        rgb = (int(r * 255), int(g * 255), int(b * 255))
        return Colour(rgb, "#%02x%02x%02x" % rgb, hue, sat, light, fo, fc, fv, fk)
