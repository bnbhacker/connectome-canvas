"""
Evolution between sittings.

The wiring is never touched. The only parameters that exist are the per-cell-type
synaptic gains (one scalar per type, all 1.0 at birth). Before each sitting a few
per cent of the types get a small multiplicative mutation; after the sitting the
trial is kept if the fly covered at least as much canvas as its parent did,
otherwise it is thrown away. That is a (1+1) evolution strategy with a noisy
fitness — the seed changes every sitting — which is exactly why the pictures
drift rather than converge.

Every generation is written to build/gains/gen-NNNN.npz and the current parent
to assets/gains.npz; each piece records its generation, so a sitting can be
replayed with the gains it was painted with.
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


class Evolution:
    def __init__(self, brain: Brain, state_path: Path = STATE_PATH, mutate_frac: float = 0.03,
                 sigma: float = 0.08, lo: float = 0.25, hi: float = 3.0):
        self.brain = brain
        self.state_path = state_path
        self.mutate_frac, self.sigma, self.lo, self.hi = mutate_frac, sigma, lo, hi
        self.generation = 0
        self.best_fitness: float | None = None
        self.accepted = 0
        self.parent = np.ones(brain.n_types, dtype=np.float32)
        self.trial: np.ndarray | None = None
        self.history: list[dict] = []
        self._load()
        brain.gains[:] = self.parent

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

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        GAINS_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(type_names=self.brain.type_names, gains=self.parent, generation=np.array(self.generation),
                       best_fitness=np.array(self.best_fitness if self.best_fitness is not None else np.nan),
                       accepted=np.array(self.accepted))
        np.savez_compressed(self.state_path, **payload)
        np.savez_compressed(GAINS_DIR / f"gen-{self.generation:04d}.npz", **payload)

    def gains_sha256(self, gains: np.ndarray | None = None) -> str:
        g = self.parent if gains is None else gains
        return hashlib.sha256(np.ascontiguousarray(g, dtype=np.float32).tobytes()).hexdigest()

    # ---- the loop --------------------------------------------------------------------

    def propose(self) -> dict:
        """Mutate the parent into a trial and install it in the brain for the coming sitting."""
        rng = np.random.default_rng(7919 * (self.generation + 1) + 17)
        trial = self.parent.copy()
        k = max(1, int(self.mutate_frac * self.brain.n_types))
        idx = rng.choice(self.brain.n_types, size=k, replace=False)
        trial[idx] *= np.exp(rng.normal(0.0, self.sigma, size=k)).astype(np.float32)
        np.clip(trial, self.lo, self.hi, out=trial)
        self.trial = trial
        self.brain.gains[:] = trial
        return {"generation": self.generation, "mutated_types": int(k), "trial_sha256": self.gains_sha256(trial)}

    def evaluate(self, fitness: float) -> dict:
        """After the sitting: keep the trial if it did at least as well as the parent. Returns the record."""
        assert self.trial is not None
        accepted = self.best_fitness is None or fitness >= self.best_fitness
        if accepted:
            self.parent = self.trial
            self.best_fitness = float(fitness)
            self.accepted += 1
        rec = {"generation": self.generation, "fitness": round(float(fitness), 6), "accepted": bool(accepted),
               "best_fitness": round(float(self.best_fitness), 6) if self.best_fitness is not None else None,
               "gains_sha256": self.gains_sha256(self.trial), "t": time.time()}
        self.generation += 1
        self.trial = None
        self.brain.gains[:] = self.parent
        self._save()
        self.history.append(rec)
        return rec

    def summary(self) -> dict:
        g = self.parent
        return {"generation": self.generation, "accepted": self.accepted, "best_fitness": self.best_fitness,
                "gains_mean": round(float(g.mean()), 4), "gains_min": round(float(g.min()), 4), "gains_max": round(float(g.max()), 4),
                "types_changed": int((np.abs(g - 1.0) > 1e-6).sum()), "types": int(g.size), "gains_sha256": self.gains_sha256()}
