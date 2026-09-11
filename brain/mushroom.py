"""
The one place a weight is allowed to move.

In a fly, learning happens at the Kenyon cell -> MBON synapse under dopamine,
and it is subtraction: a Kenyon cell that was active shortly before a
dopaminergic neuron fired has that synapse depressed. There is no potentiation
here, only depression with a floor and a slow drift back that stands in for
forgetting.

Which MBONs sit on the reward side and which on the punishment side is not
typed in from a table: for every MBON the total PAM input is compared with the
total PPL1 input, and the stronger wins.

The dopamine signal itself is invented — a fly is rewarded by sugar, not by
covering fresh canvas. The painter rewards novelty (new pixels painted) and
punishes hitting the edge. That is a modelling choice made by a person.
"""
from __future__ import annotations

import numpy as np

from .sim import Brain


class MushroomBody:
    def __init__(self, brain: Brain, depress: float = 0.05, floor: float = 0.2, forget: float = 0.001):
        self.brain = brain
        b = brain
        kc = b.where(type_re=r"^KC")
        mbon = b.where(type_re=r"^MBON")
        pam = b.where(type_re=r"^PAM")
        ppl1 = b.where(type_re=r"^PPL1")
        self.n_kc, self.n_mbon = int(kc.size), int(mbon.size)
        self.depress, self.floor, self.forget = depress, floor, forget

        is_mbon = np.zeros(b.n, dtype=bool)
        is_mbon[mbon] = True

        # every CSC entry whose column is a Kenyon cell and whose row is an MBON
        entries, kc_of, mbon_of = [], [], []
        for j in kc:
            s, e = b.indptr[j], b.indptr[j + 1]
            rows = b.indices[s:e]
            hit = np.flatnonzero(is_mbon[rows])
            if hit.size:
                entries.append(s + hit)
                kc_of.append(np.full(hit.size, j, dtype=np.int64))
                mbon_of.append(rows[hit])
        if entries:
            self.entry = np.concatenate(entries)
            self.kc_of = np.concatenate(kc_of)
            self.mbon_of = np.concatenate(mbon_of)
        else:
            self.entry = np.zeros(0, dtype=np.int64)
            self.kc_of = np.zeros(0, dtype=np.int64)
            self.mbon_of = np.zeros(0, dtype=np.int64)
        self.base = b.wdata[self.entry].copy()
        self.eff = np.ones(self.entry.size, dtype=np.float32)

        # reward-side vs punishment-side MBONs by dopaminergic input weight
        pam_in = self._input_from(pam, is_mbon)
        ppl_in = self._input_from(ppl1, is_mbon)
        reward_mbon = np.zeros(b.n, dtype=bool)
        punish_mbon = np.zeros(b.n, dtype=bool)
        for m in mbon:
            if pam_in[m] >= ppl_in[m] and pam_in[m] > 0:
                reward_mbon[m] = True
            elif ppl_in[m] > 0:
                punish_mbon[m] = True
        if not reward_mbon.any() and mbon.size:      # no dopamine wiring found: split by index
            reward_mbon[mbon[: len(mbon) // 2]] = True
            punish_mbon[mbon[len(mbon) // 2:]] = True
        self.to_reward = reward_mbon[self.mbon_of] if self.mbon_of.size else np.zeros(0, dtype=bool)
        self.to_punish = punish_mbon[self.mbon_of] if self.mbon_of.size else np.zeros(0, dtype=bool)
        self.n_reward_syn = int(self.to_reward.sum())
        self.n_punish_syn = int(self.to_punish.sum())
        self.rewards = 0
        self.punishments = 0

    def _input_from(self, src: np.ndarray, target_mask: np.ndarray) -> np.ndarray:
        b = self.brain
        acc = np.zeros(b.n, dtype=np.float32)
        for j in src:
            s, e = b.indptr[j], b.indptr[j + 1]
            rows = b.indices[s:e]
            hit = target_mask[rows]
            np.add.at(acc, rows[hit], np.abs(b.wdata[s:e][hit]))
        return acc

    def dopamine(self, kc_counts_full: np.ndarray, reward: float, punish: float) -> int:
        """Apply one window of dopamine. Returns the number of synapses currently below baseline."""
        if self.entry.size == 0:
            return 0
        fired = kc_counts_full[self.kc_of] > 0
        if reward > 0:
            m = fired & self.to_reward
            self.eff[m] *= np.float32(1.0 - self.depress * min(1.0, reward))
            self.rewards += 1
        if punish > 0:
            m = fired & self.to_punish
            self.eff[m] *= np.float32(1.0 - self.depress * min(1.0, punish))
            self.punishments += 1
        np.maximum(self.eff, self.floor, out=self.eff)
        self.eff += (1.0 - self.eff) * np.float32(self.forget)
        self.brain.wdata[self.entry] = self.base * self.eff
        return int((self.eff < 0.999).sum())

    def mean_gain(self) -> float:
        return float(self.eff.mean()) if self.eff.size else 1.0
