"""
Evolution between sittings.

Two things evolve, and the wiring is never one of them.

  gains   one scalar per cell type (11,914 of them), all 1.0 at birth: the only free
          parameters of the brain itself. A few per cent of the types get a small
          multiplicative mutation before each sitting.
  style   a dozen numbers that belong to the brush, not the fly: how hard the
          steering neurons turn it, how far forward drive carries it, whether the
          sitting is painted bilaterally, where the colour wheel starts and how far
          brain state can swing it, how dark and how tinted the paper is.

After the sitting the trial is kept when it scored at least about as well as the
current parent — (1+1) evolution with a noisy fitness, where the bar erodes a
little every time a trial fails, so a lucky peak can never freeze the lineage.
Fitness = canvas covered + a bonus for using more of the colour wheel.

Every generation is written to build/gains/gen-NNNN.npz, the parent to
assets/gains.npz; each piece records its generation, its gains hash and its
style genes, so a sitting can be replayed with exactly what it was painted with.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np

from .sim import Brain, ROOT

GAINS_DIR = ROOT / "build" / "gains"
STATE_PATH = ROOT / "assets" / "gains.npz"

# name: (default, low, high, sigma). `hue_offset` and `paper_hue` wrap around the wheel.
GENES: dict[str, tuple[float, float, float, float]] = {
    "turn_gain":  (0.35, 0.06, 1.30, 0.08),   # rad per window at full DNa02 asymmetry
    "inertia":    (0.30, 0.00, 0.92, 0.08),   # how much the heading remembers its last turn
    "step_gain":  (44.0, 10.0, 95.0, 6.0),    # px per window at full DNa01 drive
    "width_gain": (26.0, 4.0, 70.0, 5.0),     # px of stroke width at full drive
    "lift_hz":    (15.0, 5.0, 45.0, 3.0),     # DNp09 rate that lifts the brush
    "mirror":     (0.50, 0.00, 1.00, 0.12),   # chance a sitting is painted bilaterally
    "hue_offset": (0.50, 0.00, 1.00, 0.08),   # where the wheel starts (wraps)
    "hue_spread": (0.80, 0.15, 1.60, 0.12),   # how far brain state swings the hue
    "sat_base":   (0.60, 0.25, 0.95, 0.06),
    "paper_hue":  (0.62, 0.00, 1.00, 0.08),   # wraps
    "paper_sat":  (0.15, 0.00, 0.55, 0.05),
    "paper_light":(0.05, 0.02, 0.16, 0.02),
}
WRAP = {"hue_offset", "paper_hue"}
HUE_BINS = 12


def default_genes() -> dict[str, float]:
    return {k: v[0] for k, v in GENES.items()}


class Evolution:
    def __init__(self, brain: Brain, state_path: Path = STATE_PATH, mutate_frac: float = 0.03,
                 sigma: float = 0.08, lo: float = 0.25, hi: float = 3.0, slack: float = 0.97, erosion: float = 0.995):
        self.brain = brain
        self.state_path = state_path
        self.mutate_frac, self.sigma, self.lo, self.hi = mutate_frac, sigma, lo, hi
        self.slack, self.erosion = slack, erosion
        self.generation = 0
        self.best_fitness: float | None = None
        self.accepted = 0
        self.parent = np.ones(brain.n_types, dtype=np.float32)
        self.parent_genes = default_genes()
        self.trial: np.ndarray | None = None
        self.trial_genes: dict[str, float] | None = None
        self.history: list[dict] = []
        self._load()
        brain.gains[:] = self.parent

    @property
    def genes(self) -> dict[str, float]:
        """The style in force right now: the trial during a sitting, the parent between sittings."""
        return self.trial_genes if self.trial_genes is not None else self.parent_genes

    # ---- persistence -------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        z = np.load(self.state_path, allow_pickle=False)
        names = z["type_names"].astype(str)
        gains = z["gains"].astype(np.float32)
        lookup = {n: i for i, n in enumerate(self.brain.type_names)}
        for name, g in zip(names, gains):
            i = lookup.get(name)
            if i is not None:
                self.parent[i] = g
        self.generation = int(z["generation"]) if "generation" in z.files else 0
        self.best_fitness = float(z["best_fitness"]) if "best_fitness" in z.files and not np.isnan(float(z["best_fitness"])) else None
        self.accepted = int(z["accepted"]) if "accepted" in z.files else 0
        if "gene_names" in z.files:
            for name, val in zip(z["gene_names"].astype(str), z["genes"].astype(np.float64)):
                if name in GENES:
                    self.parent_genes[name] = float(val)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        GAINS_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(type_names=self.brain.type_names, gains=self.parent, generation=np.array(self.generation),
                       best_fitness=np.array(self.best_fitness if self.best_fitness is not None else np.nan),
                       accepted=np.array(self.accepted),
                       gene_names=np.array(list(self.parent_genes)), genes=np.array(list(self.parent_genes.values()), dtype=np.float64))
        np.savez_compressed(self.state_path, **payload)
        np.savez_compressed(GAINS_DIR / f"gen-{self.generation:04d}.npz", **payload)

    def gains_sha256(self, gains: np.ndarray | None = None) -> str:
        g = self.parent if gains is None else gains
        return hashlib.sha256(np.ascontiguousarray(g, dtype=np.float32).tobytes()).hexdigest()

    # ---- the loop --------------------------------------------------------------------

    def propose(self) -> dict:
        """Mutate the parent (gains and style) into a trial and install it for the coming sitting."""
        rng = np.random.default_rng(7919 * (self.generation + 1) + 17)
        trial = self.parent.copy()
        k = max(1, int(self.mutate_frac * self.brain.n_types))
        idx = rng.choice(self.brain.n_types, size=k, replace=False)
        trial[idx] *= np.exp(rng.normal(0.0, self.sigma, size=k)).astype(np.float32)
        np.clip(trial, self.lo, self.hi, out=trial)

        genes = dict(self.parent_genes)
        for name, (_, lo, hi, sig) in GENES.items():
            if rng.random() < 0.6:
                v = genes[name] + rng.normal(0.0, sig)
                genes[name] = float(v % 1.0) if name in WRAP else float(min(hi, max(lo, v)))
        self.trial, self.trial_genes = trial, genes
        self.brain.gains[:] = trial
        return {"generation": self.generation, "mutated_types": int(k), "trial_sha256": self.gains_sha256(trial),
                "genes": genes}

    def evaluate(self, fitness: float) -> dict:
        """After the sitting: keep the trial if it did about as well as the parent; otherwise let the bar erode."""
        assert self.trial is not None and self.trial_genes is not None
        bar = None if self.best_fitness is None else self.best_fitness * self.slack
        accepted = bar is None or fitness >= bar
        if accepted:
            self.parent, self.parent_genes = self.trial, self.trial_genes
            self.best_fitness = float(fitness)
            self.accepted += 1
        else:
            self.best_fitness = float(self.best_fitness) * self.erosion
        rec = {"generation": self.generation, "fitness": round(float(fitness), 6), "accepted": bool(accepted),
               "best_fitness": round(float(self.best_fitness), 6) if self.best_fitness is not None else None,
               "gains_sha256": self.gains_sha256(self.trial), "genes": dict(self.trial_genes), "t": time.time()}
        self.generation += 1
        self.trial, self.trial_genes = None, None
        self.brain.gains[:] = self.parent
        self._save()
        self.history.append(rec)
        return rec

    def summary(self) -> dict:
        g = self.parent
        return {"generation": self.generation, "accepted": self.accepted, "best_fitness": self.best_fitness,
                "gains_mean": round(float(g.mean()), 4), "gains_min": round(float(g.min()), 4), "gains_max": round(float(g.max()), 4),
                "types_changed": int((np.abs(g - 1.0) > 1e-6).sum()), "types": int(g.size), "gains_sha256": self.gains_sha256(),
                "genes": {k: round(v, 3) for k, v in self.genes.items()}}
