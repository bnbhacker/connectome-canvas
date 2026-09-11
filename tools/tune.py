"""
Parameter sweep for the model's additions (adaptation, synaptic scaling, E/I balance,
retinal tone) and the homeostat. Writes nothing into gallery/; prints one line per run.

    python tools/tune.py --ticks 150 --cap 180 --inh 2 --tone 0.25 --target 0.3
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import canvas.painter as P
from brain.sim import Brain, Params, resolve_graph
from canvas.painter import Painter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cap", type=float, default=180.0)
    ap.add_argument("--inh", type=float, default=1.0)
    ap.add_argument("--adapt", type=float, default=3.0)
    ap.add_argument("--tau", type=float, default=200.0)
    ap.add_argument("--tone", type=float, default=0.25)
    ap.add_argument("--target", type=float, default=0.3, help="mV/step into the median lamina target at full contrast")
    ap.add_argument("--gain-start", type=float, default=1.0)
    ap.add_argument("--gain-max", type=float, default=1.3)
    ap.add_argument("--noise", type=float, default=0.001)
    ap.add_argument("--graph", default=None)
    ap.add_argument("--keep", action="store_true", help="print the temp png path")
    a = ap.parse_args()

    P.GAIN_START, P.GAIN_MAX, P.NOISE_FRACTION = a.gain_start, a.gain_max, a.noise
    graph = resolve_graph(a.graph)
    brain = Brain(graph, params=Params(in_cap_mv=a.cap, adapt_mv=a.adapt, tau_adapt=a.tau, inh_scale=a.inh))
    tmp = Path(tempfile.mkdtemp())
    painter = Painter(brain, tick_ms=10.0, budget_ticks=a.ticks, graph_path=graph, gallery=tmp, recordings=tmp)
    painter.retina.tone = np.float32(a.tone)
    painter.retina.k_on *= a.target / painter.retina.target_mv
    painter.retina.k_off *= a.target / painter.retina.target_mv
    painter.new_session(a.seed)
    t0 = time.time()
    dn_idx = {"L": painter.motor.dna02_l, "R": painter.motor.dna02_r, "fwd": painter.motor.dna01,
              "mdn": painter.motor.mdn, "stop": painter.motor.dnp09}
    dn_rate = {k: 0.0 for k in dn_idx}
    dn_depol = {k: 0.0 for k in dn_idx}
    hist, comp, lr_diff, dn_win = [], np.zeros(4), 0, 0
    lifts = 0
    turns = []
    pal = painter.palette
    while painter.tick():
        c = painter.last_counts
        hist.append(int(c.sum()))
        for k, idx in dn_idx.items():
            dn_rate[k] += float(c[idx].mean()) * 100.0 if idx.size else 0.0
            dn_depol[k] += float((brain.v[idx] - brain.p.v_rest).mean()) if idx.size else 0.0
        l, r = float(c[dn_idx["L"]].sum()), float(c[dn_idx["R"]].sum())
        if abs(painter.last_cmd.turn) > 0.05:
            lr_diff += 1
        turns.append(painter.last_cmd.turn)
        if l + r + c[dn_idx["fwd"]].sum() > 0:
            dn_win += 1
        f = c > 0
        comp += [f[pal.optic].sum(), f[pal.central].sum(), f[pal.vnc].sum(), 0]
        lifts += int(painter.last_cmd.lift)
    piece = painter.finished[-1]
    n = max(1, a.ticks)
    sh = np.array(hist)
    comp = comp / n
    print(f"cap={a.cap:g} inh={a.inh:g} adapt={a.adapt:g}/{a.tau:g} tone={a.tone:g} tgt={a.target:g} | mean {piece['mean_rate_hz']:.2f} Hz "
          f"| spk/win p10 {int(np.percentile(sh, 10))} p50 {int(np.percentile(sh, 50))} p90 {int(np.percentile(sh, 90))} max {sh.max()} "
          f"| firing/win optic {comp[0]:.0f} central {comp[1]:.0f} vnc {comp[2]:.0f} "
          f"| DN Hz L/R/fwd/mdn/stop {dn_rate['L']/n:.1f}/{dn_rate['R']/n:.1f}/{dn_rate['fwd']/n:.1f}/{dn_rate['mdn']/n:.1f}/{dn_rate['stop']/n:.1f} "
          f"| DN depol mV L/R/fwd {dn_depol['L']/n:+.2f}/{dn_depol['R']/n:+.2f}/{dn_depol['fwd']/n:+.2f} "
          f"| DN active win {dn_win}/{n} turning win {lr_diff} mean|turn| {np.mean(np.abs(turns)):.2f} "
          f"| strokes {piece['strokes']} liftwin {lifts} rev {piece['reversals']} path {piece['path_px']:.0f}px "
          f"| gain {piece['gain_mean']:.2f}->{piece['gain_final']:.2f} | {time.time() - t0:.1f}s"
          + (f" | {tmp / piece['png']}" if a.keep else ""))


if __name__ == "__main__":
    main()
