"""
Colour is not a fly decision — a fly has no notion of paint. It is read off
the shape of the activity, and it moves when the activity moves.

Five population signals are taken every window: the share of spikes in the
central brain, the ON-versus-OFF balance of the medulla (Mi1/Tm3 against
Tm1/Tm2/Tm4/Tm9 — the two halves of motion vision), the VNC share, the
Kenyon-cell share and the total loudness. Each is turned into a z-score against
its own history over the sitting, so what colours the stroke is *change* in the
brain, not its resting proportions (which are 99 % optic lobe and would paint
everything the same teal).

  hue         starts at the style gene `hue_offset` and swings up to
              ±`hue_spread`/2 around the wheel with the central, ON/OFF and VNC z-scores
  saturation  `sat_base`, pushed by the Kenyon-cell z-score
  lightness   this window's loudness against the running median — a network-wide
              burst leaves a near-white mark

The mapping is a person's choice, it is fixed and public; the genes that
parameterise it evolve under our fitness, not the fly's.
"""
from __future__ import annotations

import colorsys
from collections import deque
from dataclasses import dataclass

import numpy as np

from .sim import Brain

ON_TYPES = ("Mi1", "Tm3")
OFF_TYPES = ("Tm1", "Tm2", "Tm4", "Tm9")


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
    f_on: float = 0.0
    f_off: float = 0.0


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
        self.on = np.fromiter((t.startswith(ON_TYPES) for t in ty), dtype=bool, count=brain.n)
        self.off = np.fromiter((t.startswith(OFF_TYPES) for t in ty), dtype=bool, count=brain.n)
        self.genes = {"hue_offset": 0.5, "hue_spread": 0.8, "sat_base": 0.6}
        self.history: dict[str, deque] = {}
        self.reset()

    def reset(self) -> None:
        self.history = {k: deque(maxlen=90) for k in ("central", "onoff", "vnc", "kc", "total")}

    def set_genes(self, genes: dict) -> None:
        self.genes = {k: float(genes.get(k, v)) for k, v in self.genes.items()}

    def _z(self, key: str, x: float) -> float:
        h = self.history[key]
        h.append(x)
        if len(h) < 6:
            return 0.0
        a = np.fromiter(h, dtype=np.float64)
        return float(np.clip((x - a.mean()) / (a.std() + 1e-9), -2.5, 2.5))

    def colour(self, counts: np.ndarray, ms: float) -> Colour:
        total = float(counts.sum())
        if total <= 0:
            return Colour((90, 90, 96), "#5a5a60", 0, 0, 0.35, 0, 0, 0, 0)
        fo = float(counts[self.optic].sum()) / total
        fc = float(counts[self.central].sum()) / total
        fv = float(counts[self.vnc].sum()) / total
        fk = float(counts[self.kc].sum()) / total
        on = float(counts[self.on].sum())
        off = float(counts[self.off].sum())
        f_on = on / (on + off + 1e-9)
        f_off = off / (on + off + 1e-9)

        zc = self._z("central", fc)
        zo = self._z("onoff", f_on - f_off)
        zv = self._z("vnc", fv)
        zk = self._z("kc", fk)
        zt = self._z("total", np.log(total))

        swing = float(np.tanh(0.6 * zc + 0.45 * zo + 0.3 * zv))          # -1 .. 1
        hue = (self.genes["hue_offset"] + 0.5 * self.genes["hue_spread"] * swing) % 1.0
        sat = float(np.clip(self.genes["sat_base"] + 0.3 * np.tanh(zk), 0.25, 1.0))
        light = float(np.clip(0.55 + 0.14 * zt, 0.28, 0.92))

        r, g, b = colorsys.hls_to_rgb(hue, light, sat)
        rgb = (int(r * 255), int(g * 255), int(b * 255))
        return Colour(rgb, "#%02x%02x%02x" % rgb, hue, sat, light, fo, fc, fv, fk, f_on, f_off)
