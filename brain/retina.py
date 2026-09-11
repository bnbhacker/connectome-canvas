"""
The eyes. 892 retinotopic hex columns look at a patch of the canvas around the
brush. The left half of that field belongs to the left eye and the right half
to the right eye, with an overlap down the middle, so paint to the right of the
brush drives the right optic lobe and paint to the left drives the left one.

Each column belongs to one L1 and one L2 cell of its eye — the lamina monopolar
cells directly behind photoreceptors R1–R6 — and the column map is the
release's own optic-lobe column assignment (`assignedOlHex1/2`), mirrored for
the left eye.

Lamina cells are graded, non-spiking neurons, so they are not driven as LIF
units here. Their signal is injected straight into their postsynaptic targets,
in proportion to the anatomical synapse counts of each L1 / L2 cell: an ON
channel through L1's targets (brighter than the surround or than a moment ago),
an OFF channel through L2's targets. Contrast is normalised locally the way a
lamina does it, and a small tonic drive stands in for the light of the room.
"""
from __future__ import annotations

import math

import numpy as np
import scipy.sparse as sp

from .sim import Brain

OVERLAP = 0.2   # columns with |x| below this are seen by both eyes


def hex_columns(n: int = 892) -> np.ndarray:
    """Axial hex lattice, the `n` cells nearest the centre, scaled to about [-1, 1]."""
    R = 18
    pts = []
    for q in range(-R, R + 1):
        for r in range(-R, R + 1):
            s = -q - r
            if max(abs(q), abs(r), abs(s)) <= R:
                pts.append((math.sqrt(3.0) * (q + r / 2.0), 1.5 * r))
    pts = np.asarray(pts, dtype=np.float32)
    order = np.argsort(np.hypot(pts[:, 0], pts[:, 1]), kind="stable")[:n]
    pts = pts[order]
    pts /= float(np.abs(pts).max())
    return pts


class Retina:
    def __init__(self, brain: Brain, n_columns: int = 892, fov_px: int = 256,
                 tone: float = 0.25, k_motion: float = 1.0, target_mv: float = 0.3):
        self.brain = brain
        self.cols = hex_columns(n_columns)
        self.n_columns = len(self.cols)
        self.fov_px = fov_px
        self.tone = np.float32(tone)
        self.k_motion = np.float32(k_motion)
        self.target_mv = float(target_mv)
        self.prev: np.ndarray | None = None
        self.layout_source = "cyclic"

        self.eyes: dict[str, dict] = {}
        for side in ("L", "R"):
            l1, lay1 = self._population("L1", side)
            l2, lay2 = self._population("L2", side)
            self.eyes[side] = {
                "l1": l1, "l2": l2,
                "l1_of_col": self._assign(l1, lay1, side),
                "l2_of_col": self._assign(l2, lay2, side),
            }
        self.l1 = np.concatenate([self.eyes["L"]["l1"], self.eyes["R"]["l1"]])
        self.l2 = np.concatenate([self.eyes["L"]["l2"], self.eyes["R"]["l2"]])
        self.layout = self.layout_source

        M_on = sum(self._projection(e["l1_of_col"]) for e in self.eyes.values())
        M_off = sum(self._projection(e["l2_of_col"]) for e in self.eyes.values())
        self.M_on, self.k_on = self._calibrate(M_on)
        self.M_off, self.k_off = self._calibrate(M_off)
        self.targets = int(((np.asarray(self.M_on.sum(axis=1)).ravel() != 0) |
                            (np.asarray(self.M_off.sum(axis=1)).ravel() != 0)).sum())
        self.last_lum = np.zeros(self.n_columns, dtype=np.float32)

    # ---- wiring ---------------------------------------------------------------

    def _population(self, name: str, side: str):
        b = self.brain
        idx = b.where(type_re=rf"^{name}(\b|_|$)")
        if idx.size == 0:
            idx = b.where(type_re=rf"^{name}")
        if idx.size and (b.side[idx] != "").any():
            sided = idx[b.side[idx] == side]
            if sided.size >= 8:
                idx = sided
        layout = None
        if idx.size >= 8 and b.hex1 is not None and b.hex2 is not None:
            h1, h2 = b.hex1[idx], b.hex2[idx]
            ok = ~(np.isnan(h1) | np.isnan(h2))
            if ok.sum() >= 8:
                idx = idx[ok]
                h1, h2 = h1[ok].astype(np.float64), h2[ok].astype(np.float64)
                xy = np.stack([math.sqrt(3.0) * (h1 + h2 / 2.0), 1.5 * h2], axis=1)
                xy -= xy.mean(axis=0)
                xy /= float(np.abs(xy).max()) + 1e-6
                self.layout_source = "hex columns"
                return idx, xy.astype(np.float32)
        if idx.size >= 8 and b.soma is not None:
            P = b.soma[idx]
            ok = ~np.isnan(P).any(axis=1)
            if ok.sum() >= 8:
                idx = idx[ok]
                P = P[ok] - P[ok].mean(axis=0)
                _, _, vt = np.linalg.svd(P, full_matrices=False)
                xy = P @ vt[:2].T
                xy /= float(np.abs(xy).max()) + 1e-6
                layout = xy.astype(np.float32)
                if self.layout_source == "cyclic":
                    self.layout_source = "soma"
        return idx, layout

    def _assign(self, idx: np.ndarray, layout: np.ndarray | None, side: str) -> np.ndarray:
        """Neuron index per column for this eye; -1 for columns outside its half of the field."""
        out = np.full(self.n_columns, -1, dtype=np.int64)
        if idx.size == 0:
            return out
        mine = self.cols[:, 0] <= OVERLAP if side == "L" else self.cols[:, 0] >= -OVERLAP
        cols = self.cols[mine].copy()
        if side == "L":
            cols[:, 0] = -cols[:, 0]          # mirror so both eyes share one retinotopic convention
        if layout is None:
            out[mine] = idx[np.arange(int(mine.sum())) % idx.size]
            return out
        # squeeze the half-field onto the eye's own column map
        cols[:, 0] = (cols[:, 0] - cols[:, 0].min()) / (np.ptp(cols[:, 0]) + 1e-6) * 2 - 1
        d = ((cols[:, None, :] - layout[None, :, :]) ** 2).sum(-1)
        out[mine] = idx[d.argmin(axis=1)]
        return out

    def _projection(self, cell_of_col: np.ndarray) -> sp.csr_matrix:
        """(n × columns) matrix: |synaptic weight| from the column's lamina cell onto each target."""
        b = self.brain
        rows, cols, vals = [], [], []
        for c, j in enumerate(cell_of_col):
            if j < 0:
                continue
            s, e = b.indptr[j], b.indptr[j + 1]
            rows.append(b.indices[s:e])
            cols.append(np.full(e - s, c, dtype=np.int64))
            vals.append(np.abs(b.wdata[s:e]))
        if not rows:
            return sp.csr_matrix((b.n, self.n_columns), dtype=np.float32)
        M = sp.csr_matrix((np.concatenate(vals).astype(np.float32),
                           (np.concatenate(rows), np.concatenate(cols))), shape=(b.n, self.n_columns))
        M.sum_duplicates()
        return M

    def _calibrate(self, M):
        M = sp.csr_matrix(M, dtype=np.float32)
        per_target = np.asarray(M.sum(axis=1)).ravel()
        nz = per_target[per_target > 0]
        k = self.target_mv / float(np.median(nz)) if nz.size else 0.0
        return M, k

    # ---- seeing -----------------------------------------------------------------

    def sample(self, lum: np.ndarray, center: tuple[float, float]) -> np.ndarray:
        """Mean luminance (0..1) per column from a luminance array `lum` (H, W) around `center`."""
        h, w = lum.shape
        half = self.fov_px / 2.0
        xs = center[0] + self.cols[:, 0] * half
        ys = center[1] + self.cols[:, 1] * half
        acc = np.zeros(self.n_columns, dtype=np.float32)
        for dx in (-2.0, 0.0, 2.0):
            for dy in (-2.0, 0.0, 2.0):
                xi = np.clip(np.rint(xs + dx), 0, w - 1).astype(np.int64)
                yi = np.clip(np.rint(ys + dy), 0, h - 1).astype(np.int64)
                acc += lum[yi, xi]
        acc /= 9.0
        self.last_lum = acc
        return acc

    def currents(self, L: np.ndarray) -> np.ndarray:
        """Per-neuron mV kick per step for this window, into the targets of L1 (ON) and L2 (OFF)."""
        if self.prev is None:
            self.prev = L.copy()
        dL = L - self.prev
        self.prev = L.copy()
        contrast = (L - L.mean()) / (L.std() + 0.05)          # local contrast normalisation
        contrast = np.clip(contrast, -1.0, 1.0)
        motion = np.clip(dL / 0.2, -1.0, 1.0) * self.k_motion
        on = np.clip(contrast, 0, None) + np.clip(motion, 0, None) + self.tone
        off = np.clip(-contrast, 0, None) + np.clip(-motion, 0, None) + self.tone
        i = self.M_on @ on.astype(np.float32) * np.float32(self.k_on)
        i += self.M_off @ off.astype(np.float32) * np.float32(self.k_off)
        return np.asarray(i, dtype=np.float32)

    def eye_image(self, size: int = 160) -> np.ndarray:
        """What the columns saw last, drawn as an (size, size, 3) uint8 hex-dot picture."""
        img = np.zeros((size, size, 3), dtype=np.uint8)
        img[:] = (12, 12, 16)
        half = size / 2.0 - 6
        xs = (size / 2.0 + self.cols[:, 0] * half).astype(int)
        ys = (size / 2.0 + self.cols[:, 1] * half).astype(int)
        v = (np.clip(self.last_lum, 0, 1) * 255).astype(np.uint8)
        for x, y, val in zip(xs, ys, v):
            img[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = (val, val, min(255, int(val) + 12))
        return img
