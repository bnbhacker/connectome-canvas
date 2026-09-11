"""
The hands. A fly does not have a brush, but it has descending neurons that
carry every walking decision from the brain to the legs, and those are the
neurons the brush listens to:

  DNa02 left vs right   steering — a fly turns by asymmetry between the pair
  DNa01                 forward walking
  MDN                   the moonwalker descending neuron — walking backwards
  DNp09                 freezing / stopping; here it lifts the brush

Two things are read from each of them, both smoothed over a few windows:
their spike rate, and their membrane depolarisation above rest. Descending
neurons drive the motor circuits below them with graded synaptic output, so a
DNa02 that is 2 mV more depolarised than its twin is already a steering
signal even before either of them spikes. Spikes still decide the discrete
events — lift and reverse.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .sim import Brain


@dataclass
class Rates:
    dna02_l: float
    dna02_r: float
    dna01: float
    mdn: float
    dnp09: float
    # membrane depolarisation above rest, mV, smoothed
    v_l: float = 0.0
    v_r: float = 0.0
    v_fwd: float = 0.0
    v_mdn: float = 0.0
    v_stop: float = 0.0


@dataclass
class Command:
    turn: float       # -1 (hard left) .. +1 (hard right)
    speed: float      # 0 .. 1
    reverse: bool
    lift: bool


class Motor:
    def __init__(self, brain: Brain, lift_hz: float = 15.0, reverse_hz: float = 20.0, smooth: float = 0.6):
        self.brain = brain
        b = brain
        self.dna02_l = b.where(type_re=r"^DNa02(\b|_|$)", side="L")
        self.dna02_r = b.where(type_re=r"^DNa02(\b|_|$)", side="R")
        if self.dna02_l.size == 0 or self.dna02_r.size == 0:
            both = b.where(type_re=r"^DNa02")
            self.dna02_l, self.dna02_r = both[: len(both) // 2], both[len(both) // 2:]
        self.dna01 = b.where(type_re=r"^DNa01(\b|_|$)")
        self.mdn = b.where(type_re=r"^MDN(\b|_|$)")
        self.dnp09 = b.where(type_re=r"^DNp09(\b|_|$)")
        self.lift_hz = lift_hz
        self.reverse_hz = reverse_hz
        self.smooth = smooth
        self.ema = Rates(0.0, 0.0, 0.0, 0.0, 0.0)
        self.found = {
            "DNa02 L": int(self.dna02_l.size), "DNa02 R": int(self.dna02_r.size),
            "DNa01": int(self.dna01.size), "MDN": int(self.mdn.size), "DNp09": int(self.dnp09.size),
        }

    def reset(self) -> None:
        self.ema = Rates(0.0, 0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def _hz(counts: np.ndarray, idx: np.ndarray, ms: float) -> float:
        if idx.size == 0:
            return 0.0
        return float(counts[idx].mean()) * 1000.0 / ms

    def _depol(self, v: np.ndarray, idx: np.ndarray) -> float:
        if idx.size == 0:
            return 0.0
        return float(np.clip(v[idx] - self.brain.p.v_rest, 0.0, None).mean())

    def read(self, counts: np.ndarray, ms: float, v: np.ndarray | None = None) -> Rates:
        if v is None:
            v = self.brain.v
        raw = Rates(
            dna02_l=self._hz(counts, self.dna02_l, ms), dna02_r=self._hz(counts, self.dna02_r, ms),
            dna01=self._hz(counts, self.dna01, ms), mdn=self._hz(counts, self.mdn, ms),
            dnp09=self._hz(counts, self.dnp09, ms),
            v_l=self._depol(v, self.dna02_l), v_r=self._depol(v, self.dna02_r), v_fwd=self._depol(v, self.dna01),
            v_mdn=self._depol(v, self.mdn), v_stop=self._depol(v, self.dnp09),
        )
        a, e = self.smooth, self.ema
        mix = lambda old, new: a * old + (1 - a) * new
        self.ema = Rates(
            mix(e.dna02_l, raw.dna02_l), mix(e.dna02_r, raw.dna02_r), mix(e.dna01, raw.dna01),
            mix(e.mdn, raw.mdn), mix(e.dnp09, raw.dnp09),
            mix(e.v_l, raw.v_l), mix(e.v_r, raw.v_r), mix(e.v_fwd, raw.v_fwd), mix(e.v_mdn, raw.v_mdn), mix(e.v_stop, raw.v_stop),
        )
        return self.ema

    @staticmethod
    def drive(hz: float, depol_mv: float) -> float:
        """One number for how hard a descending neuron is pushing: spikes count for more than depolarisation."""
        return hz / 40.0 + depol_mv / 3.0

    def command(self, r: Rates) -> Command:
        dl, dr = self.drive(r.dna02_l, r.v_l), self.drive(r.dna02_r, r.v_r)
        turn = (dr - dl) / (dr + dl + 0.05)
        fwd = self.drive(r.dna01, r.v_fwd)
        speed = fwd / (fwd + 0.6)                     # saturating, 0..1
        reverse = r.mdn > self.reverse_hz and r.mdn > r.dna01
        lift = r.dnp09 > self.lift_hz and r.dnp09 > r.dna01 * 0.5
        return Command(turn=float(np.clip(turn, -1, 1)), speed=float(np.clip(speed, 0, 1)),
                       reverse=bool(reverse), lift=bool(lift))
