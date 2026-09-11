"""
Leaky integrate-and-fire simulation over the male CNS connectome.

The model is the one from Shiu et al. 2024 (Nature), which reproduced
sugar-evoked proboscis extension from connectivity alone:

  * every neuron is a LIF unit with the same passive parameters
  * one presynaptic spike adds  sign * n_synapses * 0.275 mV  to each target
  * sign comes from the predicted neurotransmitter, not from fitting

Anatomy is fixed. The only free parameters are `gains`: one scalar per cell
type that scales that type's outgoing weights. Electron microscopy cannot
measure synaptic efficacy, so that is the honest place to leave a knob.

Propagation is a ragged gather over the CSC columns of the neurons that spiked
this step, so the cost tracks the number of firing neurons, not the 165k total.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import scipy.sparse as sp

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
GRAPH = BUILD / "graph.npz"
SURROGATE = BUILD / "surrogate.npz"


def resolve_graph(explicit: str | Path | None = None) -> Path:
    """The graph to run: an explicit path, else CANVAS_GRAPH, else the real graph, else the surrogate."""
    import os
    if explicit:
        return Path(explicit)
    env = os.environ.get("CANVAS_GRAPH")
    if env:
        return Path(env)
    if GRAPH.exists():
        return GRAPH
    if SURROGATE.exists():
        return SURROGATE
    raise SystemExit("no graph found. `python run.py build` for the connectome, or `python run.py surrogate` for a stand-in.")


@dataclass(frozen=True)
class Params:
    v_rest: float = -52.0      # mV
    v_thresh: float = -45.0    # mV
    v_reset: float = -52.0     # mV
    tau_m: float = 20.0        # ms
    refractory: float = 2.2    # ms
    dt: float = 0.2            # ms
    # Spike-frequency adaptation: every spike raises the neuron's own threshold by
    # `adapt_mv`, and that raise decays with `tau_adapt`. Nearly every real neuron
    # does this; Shiu et al.'s LIF does not, and without it the dense, net-excitatory
    # connectome seizes under steady input. Set adapt_mv = 0 to switch it off.
    adapt_mv: float = 3.0      # mV per spike
    tau_adapt: float = 200.0   # ms
    # Synaptic scaling: cap the summed |weight| a neuron receives from all its
    # inputs at `in_cap_mv` (so if every input spiked at once it would get at most
    # this much). Neurons under the cap keep their raw anatomy. Real neurons keep
    # their total synaptic drive in range the same way (homeostatic scaling); the
    # raw counts give hub neurons hundreds of mV per volley and the network seizes.
    in_cap_mv: float = 180.0
    # E/I balance: multiply inhibitory (GABA / glutamate) weights by this before
    # scaling. 1.0 is the anatomy as released. The counts alone give a
    # net-excitatory network that fires in network-wide avalanches; real fly
    # inhibition is stronger per synapse than the count suggests (Shiu et al.
    # note the same when matching behaviour).
    inh_scale: float = 1.0


class Brain:
    """One connectome, one membrane vector, one step() at a time."""

    def __init__(self, graph_path: Path = GRAPH, params: Params = Params(), seed: int = 0):
        z = np.load(graph_path, allow_pickle=False)
        shape = tuple(int(s) for s in z["shape"])
        W = sp.csr_matrix((z["data"].astype(np.float32), z["indices"], z["indptr"]), shape=shape)
        csc = W.tocsc()
        self.indptr = csc.indptr.astype(np.int64)
        self.indices = csc.indices.astype(np.int64)
        self.wdata = csc.data.astype(np.float32)
        self.n_edges = int(self.wdata.size)
        self.n_scaled = 0
        if params.inh_scale != 1.0:
            neg = self.wdata < 0
            self.wdata[neg] *= np.float32(params.inh_scale)
        if params.in_cap_mv > 0:
            incoming = np.bincount(self.indices, weights=np.abs(self.wdata), minlength=shape[0])
            scale = np.ones(shape[0], dtype=np.float32)
            over = incoming > params.in_cap_mv
            scale[over] = (params.in_cap_mv / incoming[over]).astype(np.float32)
            self.wdata *= scale[self.indices]
            self.n_scaled = int(over.sum())
        self.n_synapses = int(z["n_synapses"]) if "n_synapses" in z.files else 0

        self.n = shape[0]
        self.bodies = z["bodies"]
        self.types = z["types"].astype(str)
        self.superclass = z["superclass"].astype(str)
        self.side = z["side"].astype(str) if "side" in z.files else np.full(self.n, "", dtype="U1")
        self.nt = z["nt"].astype(str) if "nt" in z.files else np.full(self.n, "unknown", dtype="U8")
        self.soma = z["soma"].astype(np.float32) if "soma" in z.files else None   # (n, 3), NaN = unknown
        self.hex1 = z["hex1"].astype(np.float32) if "hex1" in z.files else None   # optic-lobe column, NaN = none
        self.hex2 = z["hex2"].astype(np.float32) if "hex2" in z.files else None
        self.graph_path = Path(graph_path)
        self.dataset = str(z["dataset"]) if "dataset" in z.files else "unknown"
        self.surrogate = bool(z["surrogate"]) if "surrogate" in z.files else False

        self.type_names, self.type_code = np.unique(self.types, return_inverse=True)
        self.type_code = self.type_code.astype(np.int64)
        self.n_types = len(self.type_names)
        self.gains = np.ones(self.n_types, dtype=np.float32)
        # One global synaptic gain. LIF units have none of the adaptation and gain
        # control real neurons have, and the released weights alone drive the
        # network into a seizure within a few ms of steady retinal input. The
        # painter nudges this scalar slowly to hold the population in a
        # physiological rate band. It is logged with every window and every piece.
        self.gain_global = np.float32(1.0)

        self.p = params
        self.decay = np.float32(np.exp(-params.dt / params.tau_m))
        self.decay_adapt = np.float32(np.exp(-params.dt / params.tau_adapt))
        self.refr_steps = int(np.ceil(params.refractory / params.dt))
        self.seed = seed
        self.rng = np.random.default_rng(seed)

        self.v = np.full(self.n, params.v_rest, dtype=np.float32)
        self.theta = np.zeros(self.n, dtype=np.float32)    # adaptive threshold offset, mV
        self.refr = np.zeros(self.n, dtype=np.int32)
        self.pending = np.zeros(self.n, dtype=np.float32)   # synaptic input landing next step
        self.t_ms = 0.0
        self.total_spikes = 0

    # ---- population selection ---------------------------------------------

    def where(self, *, type_re: str | None = None, types: list[str] | None = None,
              superclass: str | None = None, side: str | None = None) -> np.ndarray:
        """Indices of neurons matching every given annotation filter."""
        mask = np.ones(self.n, dtype=bool)
        if type_re is not None:
            rx = re.compile(type_re)
            mask &= np.fromiter((bool(rx.search(t)) for t in self.types), dtype=bool, count=self.n)
        if types is not None:
            mask &= np.isin(self.types, np.asarray(types, dtype=self.types.dtype))
        if superclass is not None:
            mask &= self.superclass == superclass
        if side is not None:
            mask &= self.side == side
        return np.flatnonzero(mask)

    def load_gains(self, path: Path) -> int:
        """Load per-type gains saved by type name; returns how many types matched."""
        z = np.load(path, allow_pickle=False)
        names = z["type_names"].astype(str)
        gains = z["gains"].astype(np.float32)
        lookup = {name: i for i, name in enumerate(self.type_names)}
        hit = 0
        for name, g in zip(names, gains):
            i = lookup.get(name)
            if i is not None:
                self.gains[i] = g
                hit += 1
        return hit

    def save_gains(self, path: Path) -> None:
        np.savez_compressed(path, type_names=self.type_names, gains=self.gains)

    # ---- dynamics -----------------------------------------------------------

    def reset(self) -> None:
        self.v.fill(self.p.v_rest)
        self.theta.fill(0.0)
        self.refr.fill(0)
        self.pending.fill(0.0)
        self.t_ms = 0.0

    def step(self, i_ext: np.ndarray | None = None) -> np.ndarray:
        """Advance one dt. `i_ext` is an mV kick per neuron this step. Returns spike indices."""
        p = self.p
        v = self.v
        v -= p.v_rest
        v *= self.decay
        v += p.v_rest
        v += self.pending
        if i_ext is not None:
            v += i_ext
        self.pending.fill(0.0)

        active = self.refr > 0
        v[active] = p.v_reset
        self.refr[active] -= 1

        if p.adapt_mv > 0:
            self.theta *= self.decay_adapt
            spk = np.flatnonzero(v >= p.v_thresh + self.theta)
        else:
            spk = np.flatnonzero(v >= p.v_thresh)
        if spk.size:
            v[spk] = p.v_reset
            self.refr[spk] = self.refr_steps
            if p.adapt_mv > 0:
                self.theta[spk] += np.float32(p.adapt_mv)
            self._propagate(spk)
            self.total_spikes += int(spk.size)
        self.t_ms += p.dt
        return spk

    def _propagate(self, spk: np.ndarray) -> None:
        starts = self.indptr[spk]
        ends = self.indptr[spk + 1]
        lens = ends - starts
        total = int(lens.sum())
        if total == 0:
            return
        # ragged range expansion: every CSC entry of every spiking column, in one shot
        offsets = np.repeat(starts - np.cumsum(lens) + lens, lens)
        flat = offsets + np.arange(total, dtype=np.int64)
        targets = self.indices[flat]
        weights = self.wdata[flat] * np.repeat(self.gains[self.type_code[spk]], lens) * self.gain_global
        self.pending += np.bincount(targets, weights=weights, minlength=self.n).astype(np.float32)

    def run(self, ms: float, drive=None) -> np.ndarray:
        """Run `ms` of brain time. `drive(step_index) -> i_ext | None`. Returns spike counts per neuron."""
        steps = int(round(ms / self.p.dt))
        counts = np.zeros(self.n, dtype=np.int32)
        for k in range(steps):
            i_ext = drive(k) if drive is not None else None
            spk = self.step(i_ext)
            if spk.size:
                counts[spk] += 1
        return counts

    @staticmethod
    def rates_hz(counts: np.ndarray, ms: float) -> np.ndarray:
        return counts.astype(np.float32) * (1000.0 / ms)
