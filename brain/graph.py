"""
Build build/graph.npz from the FlyEM male CNS connectome v1.0.

Data: HHMI Janelia FlyEM, the Cambridge Connectomics Group and Google Research,
released under CC-BY 4.0 at gs://flyem-male-cns. Three flat files are enough:

  connectome-weights-male-cns-v1.0-minconf-0.5-traced-only.feather   pre -> post synapse counts
  body-annotations-male-cns-v1.0-minconf-0.5.feather                 type / superclass / side / soma
  body-neurotransmitters-male-cns-v1.0.feather                        consensus transmitter per body

    python run.py fetch        # ~540 MB, no account, no key
    python run.py build        # -> build/graph.npz

Sign convention follows Shiu et al. 2024: acetylcholine excites, GABA and
glutamate inhibit, monoamines get zero fast weight rather than a made-up sign.
Pairs with fewer than three synapses are dropped as reconstruction noise.

Column names in the release are detected rather than assumed; `python run.py
describe` prints what the files actually contain.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BUILD = ROOT / "build"

BUCKET = "https://storage.googleapis.com/flyem-male-cns/v1.0/connectome-data/flat-connectome/"
FILES = {
    "weights": "connectome-weights-male-cns-v1.0-minconf-0.5-traced-only.feather",
    "annotations": "body-annotations-male-cns-v1.0-minconf-0.5.feather",
    "nt": "body-neurotransmitters-male-cns-v1.0.feather",
}
DATASET = "FlyEM male CNS connectome v1.0 (minconf 0.5, traced only)"

MV_PER_SYNAPSE = 0.275   # Shiu et al. 2024
MIN_SYN = 3

SIGN = {
    "acetylcholine": +1.0, "ach": +1.0,
    "gaba": -1.0,
    "glutamate": -1.0, "glut": -1.0,
    "histamine": -1.0,           # inhibitory at fly photoreceptor synapses
    "dopamine": 0.0, "da": 0.0,
    "octopamine": 0.0, "oct": 0.0,
    "serotonin": 0.0, "5ht": 0.0, "ser": 0.0,
    "tyramine": 0.0,
    "unclear": 0.0, "unknown": 0.0, "": 0.0,
}


def _pick(columns, *candidates, required=False):
    lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    if required:
        raise SystemExit(f"none of {candidates} found among columns {list(columns)}")
    return None


def fetch(only_missing: bool = True) -> None:
    import requests
    DATA.mkdir(exist_ok=True)
    for key, name in FILES.items():
        dst = DATA / name
        if only_missing and dst.exists() and dst.stat().st_size > 0:
            print(f"have {name}")
            continue
        url = BUCKET + name
        print(f"fetching {name} ...")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(dst, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r  {done / 1e6:8.1f} / {total / 1e6:.1f} MB", end="")
            print()


def describe() -> None:
    import pyarrow.feather as pf
    for key, name in FILES.items():
        p = DATA / name
        if not p.exists():
            print(f"[{key}] missing: {p}")
            continue
        tbl = pf.read_table(p)
        print(f"[{key}] {name}: {tbl.num_rows:,} rows")
        for field in tbl.schema:
            print(f"    {field.name}: {field.type}")


def _soma_from_annotations(ann, bodies: np.ndarray) -> np.ndarray | None:
    """Return (n,3) float32 soma positions aligned to `bodies`, NaN where unknown."""
    import pandas as pd
    cols = list(ann.columns)
    xyz = None
    cx = _pick(cols, "somaX", "soma_x", "x")
    cy = _pick(cols, "somaY", "soma_y", "y")
    cz = _pick(cols, "somaZ", "soma_z", "z")
    if cx and cy and cz:
        xyz = ann[[cx, cy, cz]].to_numpy(dtype=np.float64, na_value=np.nan)
    else:
        loc = _pick(cols, "somaLocation", "soma_location", "somaPosition", "position", "soma")
        if loc is not None:
            vals = ann[loc]
            out = np.full((len(ann), 3), np.nan)
            for i, v in enumerate(vals.tolist()):
                if v is None:
                    continue
                if isinstance(v, str):
                    parts = [p for p in v.replace("[", " ").replace("]", " ").replace(",", " ").split() if p]
                    if len(parts) >= 3:
                        try:
                            out[i] = [float(parts[0]), float(parts[1]), float(parts[2])]
                        except ValueError:
                            pass
                elif hasattr(v, "__len__") and len(v) >= 3:
                    out[i] = [float(v[0]), float(v[1]), float(v[2])]
            xyz = out
    if xyz is None:
        return None
    body_col = _pick(cols, "bodyId", "body", "bodyid", required=True)
    s = pd.DataFrame(xyz, index=ann[body_col].to_numpy(), columns=["x", "y", "z"])
    s = s[~s.index.duplicated(keep="first")]
    aligned = s.reindex(bodies).to_numpy(dtype=np.float32)
    known = ~np.isnan(aligned).any(axis=1)
    if known.sum() == 0:
        return None
    # scale into a 0..1000 box so the site never has to know the voxel size
    lo = np.nanmin(aligned, axis=0)
    hi = np.nanmax(aligned, axis=0)
    span = float(np.max(hi - lo)) or 1.0
    aligned = (aligned - lo) / span * 1000.0
    return aligned


def _side_from(ann, cols) -> np.ndarray:
    side_col = _pick(cols, "somaSide", "side", "soma_side", "hemisphere", "rootSide")
    inst_col = _pick(cols, "instance", "name")
    n = len(ann)
    side = np.full(n, "", dtype="U1")
    if side_col is not None:
        raw = ann[side_col].astype("string").fillna("").str.upper().to_numpy()
        side = np.array([s[:1] if s[:1] in ("L", "R") else "" for s in raw], dtype="U1")
    if inst_col is not None:
        inst = ann[inst_col].astype("string").fillna("").to_numpy()
        for i in range(n):
            if side[i] == "":
                s = str(inst[i])
                if s.endswith("_L") or s.endswith("(L)") or s.endswith(" L"):
                    side[i] = "L"
                elif s.endswith("_R") or s.endswith("(R)") or s.endswith(" R"):
                    side[i] = "R"
    return side


def build(out: Path = BUILD / "graph.npz") -> Path:
    import pandas as pd
    import pyarrow.compute as pc
    import pyarrow.feather as pf
    import scipy.sparse as sp

    BUILD.mkdir(exist_ok=True)
    wpath = DATA / FILES["weights"]
    apath = DATA / FILES["annotations"]
    npath = DATA / FILES["nt"]
    for p in (wpath, apath, npath):
        if not p.exists():
            raise SystemExit(f"missing {p}; run `python run.py fetch` first")

    print("loading annotations ...")
    ann = pd.read_feather(apath)
    cols = list(ann.columns)
    body_col = _pick(cols, "bodyId", "body", "bodyid", required=True)
    type_col = _pick(cols, "type", "cellType", "cell_type")
    fw_col = _pick(cols, "flywireType", "flywire_type")
    inst_col = _pick(cols, "instance", "name")
    status_col = _pick(cols, "status")
    label_col = _pick(cols, "statusLabel", "status_label")
    super_col = _pick(cols, "superclass", "superClass", "class")

    t = ann[type_col].astype("string") if type_col else pd.Series([pd.NA] * len(ann), dtype="string")
    if fw_col:
        t = t.fillna(ann[fw_col].astype("string"))
    if inst_col:
        t = t.fillna(ann[inst_col].astype("string"))
    ann["_type"] = t.fillna("").astype(str)
    ann["_super"] = ann[super_col].astype("string").fillna("").astype(str) if super_col else ""

    keep = np.ones(len(ann), dtype=bool)
    if status_col is not None:
        st = ann[status_col].astype("string").fillna("")
        traced = (st == "Traced").to_numpy()
        if not traced.any():
            traced = st.str.lower().str.contains("traced").to_numpy()
        if traced.any():
            keep &= traced
    if label_col is not None:
        lbl = ann[label_col].astype("string").fillna("").str.lower()
        keep &= ~lbl.str.contains("glia").to_numpy()
    ann = ann.loc[keep].drop_duplicates(subset=[body_col]).reset_index(drop=True)
    bodies = np.sort(ann[body_col].to_numpy().astype(np.int64))
    n = len(bodies)
    print(f"  {n:,} traced neurons")

    print("loading weights ...")
    tbl = pf.read_table(wpath)
    wcols = tbl.column_names
    pre_col = _pick(wcols, "body_pre", "bodyId_pre", "pre", "bodyid_pre", required=True)
    post_col = _pick(wcols, "body_post", "bodyId_post", "post", "bodyid_post", required=True)
    w_col = _pick(wcols, "weight", "count", "synapses", "n_syn", required=True)
    print(f"  {tbl.num_rows:,} pre->post pairs on disk")
    tbl = tbl.filter(pc.greater_equal(tbl.column(w_col), MIN_SYN))
    pre = tbl.column(pre_col).to_numpy().astype(np.int64)
    post = tbl.column(post_col).to_numpy().astype(np.int64)
    wt = tbl.column(w_col).to_numpy().astype(np.float32)
    del tbl
    print(f"  {len(wt):,} pairs with >= {MIN_SYN} synapses")

    idx = pd.Series(np.arange(n, dtype=np.int64), index=bodies)
    pre_i = idx.reindex(pre).to_numpy()
    post_i = idx.reindex(post).to_numpy()
    ok = ~(np.isnan(pre_i) | np.isnan(post_i))
    pre_i = pre_i[ok].astype(np.int64)
    post_i = post_i[ok].astype(np.int64)
    wt = wt[ok]
    print(f"  {len(wt):,} neuron->neuron edges, {int(wt.sum()):,} synapses")

    print("loading neurotransmitters ...")
    nt = pd.read_feather(npath)
    ncols = list(nt.columns)
    nb_col = _pick(ncols, "body", "bodyId", "bodyid", required=True)
    nn_col = _pick(ncols, "consensus_nt", "consensusNt", "nt", "predicted_nt", "top_nt", required=True)
    nt = nt[[nb_col, nn_col]].dropna(subset=[nb_col]).drop_duplicates(subset=[nb_col])
    nt_map = nt.set_index(nb_col)[nn_col].astype("string")
    nt_str = nt_map.reindex(bodies).fillna("unknown").str.lower().to_numpy().astype("U16")
    sign = np.array([SIGN.get(s, 0.0) for s in nt_str], dtype=np.float32)
    print(f"  sign: {int((sign > 0).sum()):,} excitatory, {int((sign < 0).sum()):,} inhibitory, "
          f"{int((sign == 0).sum()):,} modulatory/unknown")

    ann_i = ann.set_index(body_col).reindex(bodies)
    types = ann_i["_type"].fillna("").to_numpy().astype("U40")
    superclass = ann_i["_super"].fillna("").to_numpy().astype("U40")
    side = _side_from(ann_i.reset_index(), list(ann_i.reset_index().columns))
    soma = _soma_from_annotations(ann, bodies)
    if soma is None:
        print("  no soma positions in the annotations; the scatter will use a layout instead")
        soma = np.full((n, 3), np.nan, dtype=np.float32)
    else:
        print(f"  soma positions for {int((~np.isnan(soma).any(axis=1)).sum()):,} neurons")

    # optic-lobe hex column of each columnar neuron, when the release assigns one
    h1c = _pick(cols, "assignedOlHex1", "olHex1", "hex1")
    h2c = _pick(cols, "assignedOlHex2", "olHex2", "hex2")
    if h1c and h2c:
        hex1 = ann_i[h1c].to_numpy(dtype=np.float64, na_value=np.nan).astype(np.float32)
        hex2 = ann_i[h2c].to_numpy(dtype=np.float64, na_value=np.nan).astype(np.float32)
        print(f"  hex column assignments for {int((~np.isnan(hex1)).sum()):,} neurons")
    else:
        hex1 = np.full(n, np.nan, dtype=np.float32)
        hex2 = np.full(n, np.nan, dtype=np.float32)

    data = sign[pre_i] * wt * np.float32(MV_PER_SYNAPSE)
    W = sp.csr_matrix((data, (pre_i, post_i)), shape=(n, n), dtype=np.float32)
    W.sum_duplicates()
    W.eliminate_zeros()

    np.savez_compressed(
        out,
        data=W.data, indices=W.indices.astype(np.int64), indptr=W.indptr.astype(np.int64),
        shape=np.array(W.shape, dtype=np.int64), n_synapses=np.array(int(wt.sum()), dtype=np.int64),
        bodies=bodies, types=types, superclass=superclass, side=side, nt=nt_str, soma=soma,
        hex1=hex1, hex2=hex2,
        dataset=np.array(DATASET), surrogate=np.array(False),
    )
    print(f"wrote {out}: {n:,} neurons, {W.nnz:,} signed edges")
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    {"fetch": fetch, "describe": describe, "build": build}[cmd]()
