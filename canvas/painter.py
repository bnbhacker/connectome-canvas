"""
The painting loop.

One *sitting* is one painting. The brush starts somewhere on a dark canvas,
the retina looks at the canvas around the brush, all neurons integrate for one
window of brain time, the descending neurons say where the brush goes next,
population activity says what colour it leaves, and the retina looks again.
The fly paints what it sees and sees what it paints.

Every window is appended to a *score*: brush position, lift, colour, width,
descending-neuron rates, spike counts and a sample of which neurons fired.
The score is enough to replay the whole painting in a browser without the
brain running, and — with the seed and graph hash in its header — enough to
re-run the brain and get the identical picture.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from brain.motor import Motor, Rates, Command
from brain.mushroom import MushroomBody
from brain.palette import Palette
from brain.retina import Retina
from brain.sim import Brain, GRAPH

ROOT = Path(__file__).resolve().parent.parent
GALLERY = ROOT / "gallery"
RECORDINGS = ROOT / "site" / "recordings"

MAX_TURN = 0.35        # rad per window at full DNa02 asymmetry
MAX_STEP = 44.0        # px per window at full DNa01 drive
MIN_STEP = 3.0         # px per window: a fly is never perfectly still
NOISE_FRACTION = 0.001 # neurons per step that receive a 2 mV background kick
NOISE_MV = 2.0
SUBSAMPLE = 2000       # neurons shown in the scatter / recorded per window
FIRED_PER_TICK = 90    # how many fired-subsample indices to keep per window in the score

# Homeostat: hold the population mean rate inside a physiological band by nudging
# the one global gain. Real cortex-like rates for a fly are a few Hz on average.
RATE_LOW_HZ = 0.3
RATE_HIGH_HZ = 5.0
GAIN_START = 1.0
GAIN_MIN, GAIN_MAX = 0.05, 1.3
GAIN_UP, GAIN_DOWN = 1.02, 0.93


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Session:
    id: int
    seed: int
    started: float
    ticks: int = 0
    strokes: int = 0
    spikes: int = 0
    lifts: int = 0
    reversals: int = 0
    bumps: int = 0
    path_px: float = 0.0
    gain_sum: float = 0.0
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0
    lifted: bool = False
    width: float = 4.0
    colour: str = "#5a5a60"
    score: list = field(default_factory=list)
    swatches: list = field(default_factory=list)


class Painter:
    def __init__(self, brain: Brain, size: int = 1024, tick_ms: float = 10.0, budget_ticks: int = 600,
                 graph_path: Path = GRAPH, gallery: Path = GALLERY, recordings: Path = RECORDINGS,
                 paper=(12, 12, 16), gains_path: Path | None = None):
        self.brain = brain
        self.size = size
        self.tick_ms = tick_ms
        self.budget_ticks = budget_ticks
        self.paper = paper
        self.gallery = gallery
        self.recordings = recordings
        self.gallery.mkdir(exist_ok=True)
        self.recordings.mkdir(parents=True, exist_ok=True)
        self.graph_path = graph_path
        self.graph_sha = _sha256(graph_path) if graph_path.exists() else ""
        self.gains_loaded = 0
        if gains_path and gains_path.exists():
            self.gains_loaded = brain.load_gains(gains_path)

        self.retina = Retina(brain)
        self.motor = Motor(brain)
        self.palette = Palette(brain)
        self.mushroom = MushroomBody(brain)
        self.subsample = self._pick_subsample()
        self.sub_class = self._classes(self.subsample)
        self.sub_xyz = self._positions(self.subsample)

        self.lock = threading.Lock()
        self.events: deque = deque(maxlen=200)
        self.session: Session | None = None
        self.img: Image.Image | None = None
        self.lum: np.ndarray | None = None
        self.painted: np.ndarray | None = None
        self.last_counts: np.ndarray | None = None
        self.last_rates = Rates(0, 0, 0, 0, 0)
        self.last_cmd = Command(0, 0, False, False)
        self.last_colour = self.palette.colour(np.zeros(brain.n, dtype=np.int32), tick_ms)
        self.last_fired_sub: np.ndarray = np.zeros(0, dtype=np.int64)
        self.last_tick_wall = 0.0
        self.synapses_changed = 0
        self.finished: list[dict] = []
        self._last_event_kind: dict[str, float] = {}
        self._rate_hist: deque = deque(maxlen=20)

    # ---- setup ---------------------------------------------------------------------

    def _pick_subsample(self) -> np.ndarray:
        b = self.brain
        rng = np.random.default_rng(12345)          # fixed: the scatter is not part of the art
        must = np.concatenate([self.motor.dna02_l, self.motor.dna02_r, self.motor.dna01,
                               self.motor.mdn, self.motor.dnp09])
        if b.soma is not None:
            ok = np.flatnonzero(~np.isnan(b.soma).any(axis=1))
        else:
            ok = np.arange(b.n)
        pool = np.setdiff1d(ok, must)
        take = max(0, SUBSAMPLE - must.size)
        rest = rng.choice(pool, size=min(take, pool.size), replace=False) if pool.size else pool
        return np.concatenate([must, rest]).astype(np.int64)

    def _classes(self, idx: np.ndarray) -> list[int]:
        # 0 optic, 1 central, 2 vnc, 3 descending (the ones the brush reads), 4 kenyon
        cls = np.where(self.palette.optic[idx], 0, np.where(self.palette.vnc[idx], 2, 1))
        cls = np.where(self.palette.kc[idx], 4, cls)
        dn = np.concatenate([self.motor.dna02_l, self.motor.dna02_r, self.motor.dna01, self.motor.mdn, self.motor.dnp09])
        cls = np.where(np.isin(idx, dn), 3, cls)
        return [int(c) for c in cls]

    def _positions(self, idx: np.ndarray) -> list[list[int]]:
        b = self.brain
        if b.soma is None:
            rng = np.random.default_rng(1)
            xyz = rng.random((idx.size, 3)) * 1000
        else:
            xyz = np.nan_to_num(b.soma[idx], nan=500.0)
        return [[int(v) for v in p] for p in xyz]

    def info(self) -> dict:
        b = self.brain
        return {
            "dataset": b.dataset,
            "surrogate": b.surrogate,
            "neurons": int(b.n),
            "edges": int(b.n_edges),
            "synapses": int(b.n_synapses),
            "graph_sha256": self.graph_sha,
            "gains_loaded": self.gains_loaded,
            "model": {"adapt_mv": b.p.adapt_mv, "tau_adapt_ms": b.p.tau_adapt, "in_cap_mv": b.p.in_cap_mv,
                      "inh_scale": b.p.inh_scale, "neurons_scaled": b.n_scaled,
                      "noise_fraction": NOISE_FRACTION, "noise_mv": NOISE_MV,
                      "rate_band_hz": [RATE_LOW_HZ, RATE_HIGH_HZ], "retina_tone": float(self.retina.tone),
                      "retina_target_mv": self.retina.target_mv, "retina_targets": self.retina.targets},
            "retina": {"columns": self.retina.n_columns, "l1": int(self.retina.l1.size),
                       "l2": int(self.retina.l2.size), "layout": self.retina.layout, "fov_px": self.retina.fov_px,
                       "eyes": 2, "targets": self.retina.targets},
            "motor": self.motor.found,
            "mushroom": {"kc": self.mushroom.n_kc, "mbon": self.mushroom.n_mbon,
                         "synapses": int(self.mushroom.entry.size),
                         "reward_side": self.mushroom.n_reward_syn, "punish_side": self.mushroom.n_punish_syn},
            "canvas": {"size": self.size, "tick_ms": self.tick_ms, "budget_ticks": self.budget_ticks},
        }

    # ---- sessions --------------------------------------------------------------------

    def _next_id(self) -> int:
        index = self._read_index()
        return (max((p["id"] for p in index), default=0) + 1)

    def _read_index(self) -> list[dict]:
        p = self.gallery / "index.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []

    def _write_index(self, items: list[dict]) -> None:
        (self.gallery / "index.json").write_text(json.dumps(items, indent=1), encoding="utf-8")

    def event(self, kind: str, text: str, min_gap_s: float = 0.0) -> None:
        now = time.time()
        if min_gap_s and now - self._last_event_kind.get(kind, 0) < min_gap_s:
            return
        self._last_event_kind[kind] = now
        self.events.appendleft({"t": now, "kind": kind, "text": text})

    def new_session(self, seed: int | None = None) -> Session:
        with self.lock:
            b = self.brain
            sid = self._next_id()
            seed = int(seed) if seed is not None else secrets.randbits(32)
            b.rng = np.random.default_rng(seed)
            b.reset()
            b.gain_global = np.float32(GAIN_START)
            self._rate_hist = deque(maxlen=20)
            self.retina.prev = None
            self.motor.reset()
            self.palette.reset()
            self.img = Image.new("RGB", (self.size, self.size), self.paper)
            self.lum = np.zeros((self.size, self.size), dtype=np.float32)
            self.painted = np.zeros((self.size, self.size), dtype=bool)
            s = Session(id=sid, seed=seed, started=time.time())
            margin = self.size * 0.2
            s.x = float(margin + b.rng.random() * (self.size - 2 * margin))
            s.y = float(margin + b.rng.random() * (self.size - 2 * margin))
            s.heading = float(b.rng.random() * 2 * math.pi)
            self.session = s
            self.synapses_changed = 0
            self.event("session", f"sitting #{sid} began · seed {seed} · brush at ({int(s.x)}, {int(s.y)})")
            return s

    def tick(self) -> bool:
        """One window of brain time. Returns True while the sitting continues."""
        s = self.session
        if s is None or self.img is None or self.lum is None or self.painted is None:
            return False
        b = self.brain
        t0 = time.perf_counter()

        L = self.retina.sample(self.lum, (s.x, s.y))
        i_ret = self.retina.currents(L)
        noise_n = max(1, int(NOISE_FRACTION * b.n))

        def drive(_k: int):
            i = i_ret.copy()
            i[b.rng.integers(0, b.n, size=noise_n)] += NOISE_MV
            return i

        counts = b.run(self.tick_ms, drive)
        rates = self.motor.read(counts, self.tick_ms, b.v)
        cmd = self.motor.command(rates)
        colour = self.palette.colour(counts, self.tick_ms)

        # ---- homeostat: one slow global gain, logged, deterministic.
        # Judged on the median of recent windows so a single network-wide burst
        # neither counts as "healthy activity" nor triggers a panic.
        mean_hz = float(counts.sum()) * 1000.0 / self.tick_ms / b.n
        self._rate_hist.append(mean_hz)
        typical = float(np.median(self._rate_hist))
        if typical > RATE_HIGH_HZ or mean_hz > 20 * RATE_HIGH_HZ:
            b.gain_global = np.float32(max(GAIN_MIN, float(b.gain_global) * GAIN_DOWN))
        elif typical < RATE_LOW_HZ:
            b.gain_global = np.float32(min(GAIN_MAX, float(b.gain_global) * GAIN_UP))
        s.gain_sum += float(b.gain_global)

        # ---- move the brush
        s.heading += cmd.turn * MAX_TURN
        step = MIN_STEP + cmd.speed * MAX_STEP
        if cmd.reverse:
            step = -step
            s.reversals += 1
            self.event("mdn", "MDN fired: the brush walks backwards", 3.0)
        nx = s.x + math.cos(s.heading) * step
        ny = s.y + math.sin(s.heading) * step
        bump = False
        pad = 8.0
        if nx < pad or nx > self.size - pad:
            s.heading = math.pi - s.heading
            nx = min(max(nx, pad), self.size - pad)
            bump = True
        if ny < pad or ny > self.size - pad:
            s.heading = -s.heading
            ny = min(max(ny, pad), self.size - pad)
            bump = True
        if bump:
            s.bumps += 1
            self.event("bump", "hit the edge of the canvas · punishment", 2.0)

        width = 3.0 + 26.0 * cmd.speed
        new_px = 0
        if cmd.lift:
            if not s.lifted:
                s.lifts += 1
                self.event("lift", "DNp09 fired: the brush lifts", 2.0)
            s.lifted = True
        else:
            if s.lifted or s.ticks == 0:
                s.strokes += 1
            s.lifted = False
            new_px = self._stroke((s.x, s.y), (nx, ny), width, colour.rgb)
        s.path_px += abs(step)
        s.x, s.y, s.width, s.colour = float(nx), float(ny), float(width), colour.hex

        # ---- dopamine: novelty rewards, the edge punishes (a modelling choice, see README)
        expected = max(1.0, abs(step) * width)
        novelty = min(1.0, new_px / expected) if not cmd.lift else 0.0
        self.synapses_changed = self.mushroom.dopamine(counts, reward=novelty if novelty > 0.6 else 0.0,
                                                       punish=1.0 if bump else 0.0)

        # ---- bookkeeping
        n_spk = int(counts.sum())
        s.spikes += n_spk
        s.ticks += 1
        fired_sub = np.flatnonzero(counts[self.subsample] > 0)
        self.last_counts = counts
        self.last_rates = rates
        self.last_cmd = cmd
        self.last_colour = colour
        self.last_fired_sub = fired_sub
        self.last_tick_wall = time.perf_counter() - t0
        if not cmd.lift and (not s.swatches or s.swatches[-1] != colour.hex):
            s.swatches.append(colour.hex)
            if len(s.swatches) > 64:
                s.swatches = s.swatches[-64:]

        keep = fired_sub if fired_sub.size <= FIRED_PER_TICK else \
            fired_sub[np.linspace(0, fired_sub.size - 1, FIRED_PER_TICK).astype(int)]
        s.score.append([
            round(s.x, 1), round(s.y, 1), int(cmd.lift), int(cmd.reverse), round(width, 1), colour.hex,
            n_spk, int((counts > 0).sum()),
            round(rates.dna02_l, 1), round(rates.dna02_r, 1), round(rates.dna01, 1),
            round(rates.mdn, 1), round(rates.dnp09, 1),
            [int(i) for i in keep],
            round(float(b.gain_global), 4),
            [round(rates.v_l, 2), round(rates.v_r, 2), round(rates.v_fwd, 2)],
        ])

        if s.ticks >= self.budget_ticks:
            self.finish()
            return False
        return True

    def _stroke(self, a: tuple[float, float], b: tuple[float, float], width: float, rgb) -> int:
        assert self.img is not None and self.lum is not None and self.painted is not None
        draw = ImageDraw.Draw(self.img)
        r = width / 2.0
        draw.line([a, b], fill=rgb, width=max(1, int(round(width))))
        for (x, y) in (a, b):
            draw.ellipse([x - r, y - r, x + r, y + r], fill=rgb)
        x0 = int(max(0, min(a[0], b[0]) - r - 1))
        y0 = int(max(0, min(a[1], b[1]) - r - 1))
        x1 = int(min(self.size, max(a[0], b[0]) + r + 2))
        y1 = int(min(self.size, max(a[1], b[1]) + r + 2))
        if x1 <= x0 or y1 <= y0:
            return 0
        region = np.asarray(self.img.crop((x0, y0, x1, y1)).convert("L"), dtype=np.float32) / 255.0
        self.lum[y0:y1, x0:x1] = region
        was = self.painted[y0:y1, x0:x1]
        now = region > 0.12
        new_px = int((now & ~was).sum())
        self.painted[y0:y1, x0:x1] = was | now
        return new_px

    def finish(self) -> dict | None:
        s = self.session
        if s is None or self.img is None:
            return None
        with self.lock:
            b = self.brain
            stem = f"canvas-{s.id:04d}"
            png_path = self.gallery / f"{stem}.png"
            self.img.save(png_path, "PNG", optimize=True)
            png_sha = _sha256(png_path)
            counts_total = self.last_counts if self.last_counts is not None else np.zeros(b.n, dtype=np.int32)
            top = self._top_types(counts_total)
            piece = {
                "id": s.id,
                "name": f"Canvas Fly #{s.id}",
                "seed": s.seed,
                "finished_at": time.time(),
                "wall_seconds": round(time.time() - s.started, 1),
                "brain_ms": round(s.ticks * self.tick_ms, 1),
                "ticks": s.ticks,
                "strokes": s.strokes,
                "lifts": s.lifts,
                "reversals": s.reversals,
                "bumps": s.bumps,
                "path_px": round(s.path_px, 1),
                "spikes": s.spikes,
                "mean_rate_hz": round(s.spikes * 1000.0 / max(1.0, s.ticks * self.tick_ms) / b.n, 3),
                "gain_final": round(float(b.gain_global), 4),
                "gain_mean": round(s.gain_sum / max(1, s.ticks), 4),
                "synapses_changed": self.synapses_changed,
                "rewards": self.mushroom.rewards,
                "punishments": self.mushroom.punishments,
                "top_types": top,
                "swatches": s.swatches[-12:],
                "png": f"{stem}.png",
                "png_sha256": png_sha,
                "recording": f"recordings/{stem}.json",
                "dataset": b.dataset,
                "surrogate": b.surrogate,
                "graph_sha256": self.graph_sha,
                "neurons": int(b.n),
                "edges": int(b.n_edges),
                "chain": None,
            }
            score = {
                "version": 1,
                "piece": {k: v for k, v in piece.items() if k not in ("recording",)},
                "size": self.size,
                "tick_ms": self.tick_ms,
                "paper": list(self.paper),
                "columns": ["x", "y", "lift", "reverse", "width", "colour", "spikes", "firing",
                            "dna02_l", "dna02_r", "dna01", "mdn", "dnp09", "fired_subsample", "gain_global",
                            "depol_mv[l,r,fwd]"],
                "neurons": {"xyz": self.sub_xyz, "cls": self.sub_class,
                            "legend": ["optic", "central", "vnc", "descending", "kenyon"]},
                "ticks": s.score,
            }
            (self.recordings / f"{stem}.json").write_text(json.dumps(score, separators=(",", ":")), encoding="utf-8")
            # a 512 px copy next to the score, so the demonstration and the gallery work as static files
            self.img.resize((512, 512), Image.LANCZOS).save(self.recordings / f"{stem}.jpg", "JPEG", quality=82)
            piece["thumb"] = f"{stem}.jpg"
            (self.gallery / f"{stem}.json").write_text(json.dumps(piece, indent=1), encoding="utf-8")
            index = self._read_index()
            index = [p for p in index if p["id"] != s.id] + [piece]
            index.sort(key=lambda p: p["id"])
            self._write_index(index)
            rec_index = [{"id": p["id"], "name": p["name"], "png": p["png"], "thumb": p.get("thumb"), "seed": p["seed"],
                          "recording": p["recording"], "strokes": p["strokes"], "spikes": p["spikes"],
                          "brain_ms": p["brain_ms"], "surrogate": p["surrogate"], "swatches": p["swatches"],
                          "png_sha256": p.get("png_sha256"), "synapses_changed": p.get("synapses_changed"),
                          "chain": p.get("chain")}
                         for p in index if (self.recordings / Path(p["recording"]).name).exists()]
            (self.recordings / "index.json").write_text(json.dumps(rec_index, indent=1), encoding="utf-8")
            self.finished.append(piece)
            self.event("finished", f"sitting #{s.id} finished · {s.strokes} strokes · {s.spikes:,} spikes · sha {png_sha[:10]}…")
            self.session = None
            return piece

    def _top_types(self, counts: np.ndarray, k: int = 8) -> list[dict]:
        b = self.brain
        sums = np.bincount(b.type_code, weights=counts, minlength=b.n_types)
        order = np.argsort(sums)[::-1][:k]
        return [{"type": str(b.type_names[i]), "spikes": int(sums[i])} for i in order if sums[i] > 0]

    # ---- views for the server -----------------------------------------------------------

    def frame_png(self, size: int = 512) -> bytes:
        import io
        with self.lock:
            img = self.img.copy() if self.img is not None else Image.new("RGB", (self.size, self.size), self.paper)
        if size != self.size:
            img = img.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=3)
        return buf.getvalue()

    def eye_png(self) -> bytes:
        import io
        arr = self.retina.eye_image()
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, "PNG")
        return buf.getvalue()

    def state(self) -> dict:
        s = self.session
        b = self.brain
        r, c, col = self.last_rates, self.last_cmd, self.last_colour
        return {
            "status": "painting" if s is not None else "between sittings",
            "surrogate": b.surrogate,
            "dataset": b.dataset,
            "neurons": int(b.n),
            "edges": int(b.n_edges),
            "synapses": int(b.n_synapses),
            "session": None if s is None else {
                "id": s.id, "seed": s.seed, "ticks": s.ticks, "budget": self.budget_ticks,
                "brain_ms": round(s.ticks * self.tick_ms, 1), "strokes": s.strokes, "spikes": s.spikes,
                "lifts": s.lifts, "reversals": s.reversals, "bumps": s.bumps, "path_px": round(s.path_px),
                "started": s.started, "swatches": s.swatches[-16:],
            },
            "brush": None if s is None else {"x": round(s.x, 1), "y": round(s.y, 1), "heading": round(s.heading, 3),
                                             "lift": s.lifted, "width": round(s.width, 1), "colour": s.colour},
            "rates": {"dna02_l": round(r.dna02_l, 1), "dna02_r": round(r.dna02_r, 1), "dna01": round(r.dna01, 1),
                      "mdn": round(r.mdn, 1), "dnp09": round(r.dnp09, 1),
                      "v_l": round(r.v_l, 2), "v_r": round(r.v_r, 2), "v_fwd": round(r.v_fwd, 2),
                      "v_mdn": round(r.v_mdn, 2), "v_stop": round(r.v_stop, 2)},
            "command": {"turn": round(c.turn, 3), "speed": round(c.speed, 3), "reverse": c.reverse, "lift": c.lift},
            "colour": {"hex": col.hex, "hue": round(col.hue, 3), "sat": round(col.sat, 3), "light": round(col.light, 3),
                       "optic": round(col.f_optic, 3), "central": round(col.f_central, 3),
                       "vnc": round(col.f_vnc, 3), "kenyon": round(col.f_kc, 3)},
            "window": {
                "spikes": int(self.last_counts.sum()) if self.last_counts is not None else 0,
                "firing": int((self.last_counts > 0).sum()) if self.last_counts is not None else 0,
                "spikes_per_s": round(float(self.last_counts.sum()) * 1000.0 / self.tick_ms) if self.last_counts is not None else 0,
                "ms": self.tick_ms,
                "wall_ms": round(self.last_tick_wall * 1000),
                "membrane_mv": round(float(b.v.mean()), 2),
                "mean_hz": round(float(self.last_counts.sum()) * 1000.0 / self.tick_ms / b.n, 2) if self.last_counts is not None else 0,
                "gain_global": round(float(b.gain_global), 4),
            },
            "mushroom": {"changed": self.synapses_changed, "mean_gain": round(self.mushroom.mean_gain(), 4),
                         "rewards": self.mushroom.rewards, "punishments": self.mushroom.punishments,
                         "synapses": int(self.mushroom.entry.size)},
            "fired": [int(i) for i in self.last_fired_sub],
            "events": list(self.events)[:40],
            "gallery_count": len(self._read_index()),
        }
