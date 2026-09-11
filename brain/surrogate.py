"""
A stand-in graph for machines that do not have the connectome yet.

It is NOT a fly brain. It is a random sparse network that carries the same
*shape of annotations* the real graph does (types, superclasses, sides, soma
positions), so every other module can be exercised end to end. The npz is
stamped `surrogate=True`, the server exposes that flag, and the site prints
"SURROGATE GRAPH — NOT THE CONNECTOME" across the page while it is loaded.
Nothing painted on a surrogate is ever minted; mint.py refuses.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import scipy.sparse as sp

from .sim import SURROGATE

# Populations the painter reads or writes, with a plausible count each.
NAMED = {
    "L1": 900, "L2": 900,
    "DNa02": 2, "DNa01": 2, "MDN": 4, "DNp09": 2,
    "KC": 2400, "MBON01": 2, "MBON02": 2, "MBON03": 2,
    "PAM01": 40, "PAM02": 40, "PAM03": 40, "PPL101": 12, "PPL102": 12,
    "T4a": 300, "T4b": 300, "T5a": 300, "T5b": 300, "LC4": 60, "LPLC2": 60,
    "Mi1": 700, "Tm3": 700, "Tm1": 700, "Tm2": 700,
}
OPTIC = {"L1", "L2", "T4a", "T4b", "T5a", "T5b", "LC4", "LPLC2", "Mi1", "Tm3", "Tm1", "Tm2"}


def build(n: int = 24000, seed: int = 7, out: Path = SURROGATE) -> Path:
    rng = np.random.default_rng(seed)
    types = np.array([f"anon{rng.integers(0, 400):03d}" for _ in range(n)], dtype="U16")
    superclass = rng.choice(np.array(["optic", "central", "vnc", "sensory", "descending"]),
                            size=n, p=[0.45, 0.30, 0.15, 0.07, 0.03]).astype("U16")
    side = rng.choice(np.array(["L", "R"]), size=n).astype("U1")

    cursor = 0
    for name, count in NAMED.items():
        types[cursor:cursor + count] = name
        if name in OPTIC:
            superclass[cursor:cursor + count] = "optic"
        elif name.startswith(("DN", "MDN")):
            superclass[cursor:cursor + count] = "descending"
        else:
            superclass[cursor:cursor + count] = "central"
        if count == 2:
            side[cursor] = "L"
            side[cursor + 1] = "R"
        else:
            half = count // 2
            side[cursor:cursor + half] = "L"
            side[cursor + half:cursor + count] = "R"
        cursor += count
    n_named = cursor

    # random signed graph, ~50 outgoing edges per neuron, >= 3 synapses each like the real cut
    k = 50
    pre = np.repeat(np.arange(n), k)
    post = rng.integers(0, n, size=n * k)
    syn = (rng.geometric(0.3, size=n * k) + 2).astype(np.float32)
    nt = rng.choice(np.array(["acetylcholine", "gaba", "glutamate"]), size=n, p=[0.62, 0.23, 0.15])
    sign = np.where(nt == "acetylcholine", 1.0, -1.0).astype(np.float32)

    # wire the retina forward: lamina -> medulla -> T4/T5 -> LC/central -> descending,
    # so a kick on L1/L2 actually reaches the neurons the brush reads
    idx = {}
    c = 0
    for name, count in NAMED.items():
        idx[name] = np.arange(c, c + count)
        c += count
    stages = [
        (np.concatenate([idx["L1"], idx["L2"]]), np.concatenate([idx["Mi1"], idx["Tm3"], idx["Tm1"], idx["Tm2"]])),
        (np.concatenate([idx["Mi1"], idx["Tm3"], idx["Tm1"], idx["Tm2"]]),
         np.concatenate([idx["T4a"], idx["T4b"], idx["T5a"], idx["T5b"]])),
        (np.concatenate([idx["T4a"], idx["T4b"], idx["T5a"], idx["T5b"]]),
         np.concatenate([idx["LC4"], idx["LPLC2"], np.flatnonzero(superclass == "central")[:1500]])),
        (np.concatenate([idx["LC4"], idx["LPLC2"], idx["KC"][:600]]),
         np.concatenate([idx["DNa02"], idx["DNa01"], idx["MDN"], idx["DNp09"], idx["MBON01"], idx["MBON02"], idx["MBON03"]])),
    ]
    for src, dst in stages:
        m = np.isin(pre, src)
        post[m] = rng.choice(dst, size=int(m.sum()))
        sign[src] = 1.0   # feed-forward excitatory so the signal survives four hops
    # everything named in the retina gets a small excitatory bias
    data = sign[pre] * syn * np.float32(0.275)
    W = sp.csr_matrix((data, (pre, post)), shape=(n, n), dtype=np.float32)
    W.sum_duplicates()

    # soma positions: blobs per superclass, split by side, in a 0..1000 box
    soma = np.empty((n, 3), dtype=np.float32)
    centers = {"optic": (0.28, 0.5, 0.5), "central": (0.55, 0.5, 0.5), "vnc": (0.84, 0.5, 0.5),
               "sensory": (0.55, 0.32, 0.5), "descending": (0.66, 0.6, 0.5)}
    scales = {"optic": (0.07, 0.11, 0.09), "central": (0.09, 0.13, 0.09), "vnc": (0.06, 0.09, 0.06),
              "sensory": (0.05, 0.05, 0.05), "descending": (0.03, 0.04, 0.04)}
    for sc, ctr in centers.items():
        m = superclass == sc
        soma[m] = rng.normal(loc=ctr, scale=scales[sc], size=(int(m.sum()), 3))
    soma[side == "L", 1] -= 0.17
    soma[side == "R", 1] += 0.17
    soma *= 1000.0

    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        data=W.data, indices=W.indices.astype(np.int64), indptr=W.indptr.astype(np.int64),
        shape=np.array(W.shape, dtype=np.int64),
        n_synapses=np.array(int(np.abs(W.data).sum() / 0.275), dtype=np.int64),
        bodies=np.arange(n, dtype=np.int64), types=types, superclass=superclass, side=side,
        nt=nt.astype("U16"), soma=soma,
        dataset=np.array("SURROGATE random graph — not the connectome"), surrogate=np.array(True),
    )
    return out


if __name__ == "__main__":
    p = build()
    print(f"wrote {p} (surrogate, not a fly)")
