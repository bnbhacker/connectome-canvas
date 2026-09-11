"""
The studio server: runs sittings back to back in a background thread and
serves the site, the live state, the current canvas and the gallery.

    python run.py serve --port 4660

Environment (all optional):
    CANVAS_GRAPH      path to graph.npz            (default build/graph.npz)
    CANVAS_TICKS      windows per sitting          (default 600)
    CANVAS_TICK_MS    brain ms per window          (default 10)
    CANVAS_PAUSE_S    pause between sittings       (default 20)
    CANVAS_MINT       1 = mint every finished piece (default 0; refused on a surrogate graph)
    CANVAS_LIST_ETH   list price after mint, in ETH (default: no listing)
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from brain.sim import Brain, resolve_graph
from canvas.painter import Painter, GALLERY

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Studio:
    """Sittings, one after another, forever."""

    def __init__(self, painter: Painter, pause_s: float = 20.0, mint: bool = False, list_eth: str | None = None,
                 max_sittings: int = 0, list_currency: str | None = None):
        self.painter = painter
        self.pause_s = pause_s
        self.mint = mint and not painter.brain.surrogate
        self.list_eth = list_eth                  # the listing price, in units of list_currency
        self.list_currency = list_currency or "ETH"
        self._list_paused_until = 0.0             # set when OpenSea wants another currency; re-checked later
        self.list_last_error: str | None = None
        self.max_sittings = max_sittings          # 0 = paint forever; N = paint N sittings, then rest
        self.done = 0
        self.resting = False
        self.sold_out = False
        self._chain_lock = threading.Lock()       # mint → publish → list, one piece at a time, in order
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="studio", daemon=True)
        self.next_sitting_at: float | None = None
        self._publish_lock = threading.Lock()     # one deploy at a time; the stage dir is shared
        from collections import deque
        # Vercel's free plan allows 100 deployments a day, so the site is published in batches
        self.publish_every = float(os.environ.get("CANVAS_PUBLISH_EVERY_S", "1200"))
        self._last_publish = 0.0
        # the free OpenSea key allows 30 writes an hour; stay under it
        self.list_per_hour = int(os.environ.get("CANVAS_LIST_PER_HOUR", "25"))
        self._list_posts: deque = deque()

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        p = self.painter
        # the cap counts every sitting ever painted, not just this process's
        already = len(p._read_index())
        while not self.stop.is_set():
            painted = already + self.done
            if (self.max_sittings and painted >= self.max_sittings) or self.sold_out:
                self.resting = True
                why = "the collection is sold out" if self.sold_out else f"{painted} sittings painted, the cap is {self.max_sittings}"
                p.event("rest", f"the fly has put the brush down: {why}")
                self.stop.wait()
                break
            p.new_session()
            while not self.stop.is_set() and p.tick():
                time.sleep(0)
            if self.stop.is_set():
                break
            self.done += 1
            piece = p.finished[-1] if p.finished else None
            if piece:
                threading.Thread(target=self._after_sitting, args=(piece["id"],), daemon=True).start()
            if self.max_sittings and already + self.done >= self.max_sittings:
                continue
            self.next_sitting_at = time.time() + self.pause_s
            self.stop.wait(self.pause_s)
            self.next_sitting_at = None

    def _after_sitting(self, piece_id: int) -> None:
        """Every CANVAS_PUBLISH_EVERY_S: write metadata for the unminted pieces → publish the site → mint them,
        so metadata is live before a token exists. Every sitting: list what is minted, within OpenSea's limit.
        Serialised under one lock so ids, files and nonces never race."""
        with self._chain_lock:
            autopub = os.environ.get("CANVAS_AUTOPUBLISH", "0") == "1"
            if autopub and time.time() - self._last_publish >= self.publish_every:
                prepared = self._prepare() if self.mint else []
                ok = self._publish(f"batch up to sitting #{piece_id}")
                self._last_publish = time.time()
                if ok and prepared:
                    self._mint_prepared(prepared)
            elif self.mint and not autopub:
                self._mint_prepared(self._prepare())      # metadata must then be hosted elsewhere (ipfs)
            if self.mint and self.list_eth:
                self._list_backlog()

    def _publish(self, what) -> bool:
        """Push site/ (scores, thumbnails, gallery index, nft metadata, live.json) to Vercel. True on success."""
        import subprocess
        import sys
        label = f"sitting #{what}" if isinstance(what, int) else str(what)
        with self._publish_lock:
            try:
                self.painter.event("publish", f"publishing {label} to the site …")
                proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "publish"], cwd=ROOT,
                                      capture_output=True, text=True, timeout=600)
                if proc.returncode == 0:
                    self.painter.event("publish", f"site updated · {label}")
                    return True
                self.painter.event("error", f"publish failed: {(proc.stderr or proc.stdout).strip()[-200:]}")
            except Exception as e:
                self.painter.event("error", f"publish failed: {type(e).__name__}: {e}")
            return False

    def _prepare(self) -> list:
        """Write metadata for every unminted piece under the token id it will get. Never raises."""
        try:
            from chain.mint import prepare_metadata
            prepared = prepare_metadata()
            if prepared:
                self.painter.event("mint", f"metadata ready for {len(prepared)} piece(s): tokens {prepared[0][1]}–{prepared[-1][1]}")
            return prepared
        except BaseException as e:  # SystemExit included
            if "sold out" in str(e):
                self.sold_out = True
            self.painter.event("error", f"metadata step failed: {str(e)[:160]}")
            return []

    def _mint_prepared(self, prepared: list) -> None:
        """Mint in order; the first failure ends the batch so ids stay aligned with the published metadata."""
        from chain.mint import mint_piece
        for piece_id, token_id in prepared:
            try:
                r = mint_piece(piece_id, dry_run=False)
                self.painter.event("mint", f"minted sitting #{piece_id} as token {r['token_id']} · {r['tx'][:12]}…")
                if r["token_id"] != token_id:
                    self.painter.event("error", f"token id drifted ({r['token_id']} ≠ {token_id}); the batch stops here")
                    break
            except BaseException as e:  # the studio keeps painting whatever happens on chain
                if "sold out" in str(e):
                    self.sold_out = True
                self.painter.event("error", f"mint failed for #{piece_id}: {str(e)[:160]}; the batch stops here")
                break

    def _list_budget(self) -> int:
        now = time.time()
        while self._list_posts and now - self._list_posts[0] > 3600:
            self._list_posts.popleft()
        return max(0, self.list_per_hour - len(self._list_posts))

    def _list_backlog(self, max_per_pass: int = 3) -> None:
        """List every minted-but-unlisted piece, oldest first, a few per pass; failures are retried next time."""
        import json as _json
        from chain.opensea import list_piece
        if time.time() < self._list_paused_until:
            return
        index = self.painter._read_index()
        todo = [p for p in index if (p.get("chain") or {}).get("token_id") is not None
                and not (p.get("chain") or {}).get("listing") and (p.get("chain") or {}).get("list_attempts", 0) < 8]
        for p in todo[:min(max_per_pass, self._list_budget())]:
            self._list_posts.append(time.time())
            try:
                self.painter.event("list", f"listing #{p['id']} (token {p['chain']['token_id']}) at {self.list_eth} {self.list_currency} …")
                listing = list_piece(p["id"], self.list_eth, currency=self.list_currency)
                self.painter.event("list", f"listed #{p['id']} · {listing.get('url', '')}")
            except BaseException as e:  # SystemExit included: OpenSea may not have indexed the token yet
                if "requires this collection to be priced in" in str(e):
                    # the currency is a person's decision: never convert, never burn retries on it; look again in 10 min
                    self._list_paused_until = time.time() + 600
                    self._list_posts.pop()            # a refusal like this is not worth a slot in the hourly budget
                    self.list_last_error = str(e)[:400]
                    # its own event kind: sharing "error" with a min-gap let an earlier error swallow this line
                    self.painter.event("list", f"automatic listing paused, re-checking in 10 min: {str(e)[:240]}")
                    break
                gp = GALLERY / f"canvas-{p['id']:04d}.json"
                try:
                    piece = _json.loads(gp.read_text(encoding="utf-8"))
                    piece.setdefault("chain", {})["list_attempts"] = piece["chain"].get("list_attempts", 0) + 1
                    from chain.mint import save_piece
                    save_piece(gp, piece)
                except Exception:
                    pass
                self.painter.event("error", f"listing #{p['id']} failed (will retry): {str(e)[:160]}")
                if "429" in str(e):      # rate-limited: leave the rest for a later pass
                    break


class Tunnel:
    """A Cloudflare quick tunnel in front of the local studio, so the public site can reach the live brain.

    No account and no DNS: cloudflared hands out a random https://….trycloudflare.com address, which
    is written to site/live.json (and published) whenever it changes. The address dies with the process."""

    URL_RE = __import__("re").compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    def __init__(self, port: int, on_ready, on_event=None, log_path: Path | None = None) -> None:
        self.port = port
        self.on_ready = on_ready
        self.on_event = on_event or (lambda kind, text: None)
        self.log_path = log_path or (ROOT / "build" / "tunnel.log")
        self.url: str | None = None
        self.proc = None
        self.restarts = 0
        self._halt = threading.Event()

    @staticmethod
    def _exe() -> str:
        import shutil
        # the real binary, not scoop's shim: killing a shim can orphan the cloudflared behind it
        real = Path.home() / "scoop" / "apps" / "cloudflared" / "current" / "cloudflared.exe"
        if real.exists():
            return str(real)
        exe = shutil.which("cloudflared")
        if exe:
            return exe
        raise RuntimeError("cloudflared not found (scoop install cloudflared)")

    def start(self) -> None:
        self._exe()                                   # fail fast when it is not installed
        threading.Thread(target=self._supervise, name="tunnel", daemon=True).start()

    def _supervise(self) -> None:
        """Keep one quick tunnel alive: log everything, announce each new address, restart with backoff."""
        import collections
        import subprocess
        backoff = 5.0
        while not self._halt.is_set():
            tail: collections.deque = collections.deque(maxlen=12)
            try:
                self.proc = subprocess.Popen(
                    [self._exe(), "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                )
            except Exception as e:
                self.on_event("error", f"tunnel could not start: {e}; retrying in {int(backoff)} s")
                self._halt.wait(backoff)
                backoff = min(300.0, backoff * 2)
                continue
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as log:
                for line in self.proc.stdout:
                    log.write(line)
                    log.flush()
                    tail.append(line.strip())
                    m = self.URL_RE.search(line)
                    # cloudflared also logs its own API host (api.trycloudflare.com); the tunnel is the other one
                    if m and self.url is None and not m.group(0).startswith("https://api."):
                        self.url = m.group(0)
                        backoff = 5.0
                        try:
                            self.on_ready(self.url)
                        except Exception as e:
                            self.on_event("error", f"tunnel announce failed: {e}")
            code = self.proc.wait()
            self.url = None
            if self._halt.is_set():
                break
            self.restarts += 1
            last = next((t for t in reversed(tail) if " ERR " in t or "error" in t.lower()), tail[-1] if tail else "")
            self.on_event("error", f"tunnel exited (code {code}); restarting in {int(backoff)} s · {last[-160:]}")
            self._halt.wait(backoff)
            backoff = min(300.0, backoff * 2)

    def stop(self) -> None:
        self._halt.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def build_app(graph_path: Path | None = None) -> FastAPI:
    graph_path = resolve_graph(graph_path)
    if not graph_path.exists():
        raise SystemExit(f"no graph at {graph_path}. Run `python run.py build` (real) or `python run.py surrogate` (dev).")
    brain = Brain(graph_path)
    gains = ROOT / "assets" / "gains.npz"
    painter = Painter(brain, tick_ms=float(os.environ.get("CANVAS_TICK_MS", 10)),
                      budget_ticks=_env_int("CANVAS_TICKS", 600), graph_path=graph_path,
                      gains_path=gains if gains.exists() else None)
    studio = Studio(painter, pause_s=float(os.environ.get("CANVAS_PAUSE_S", 20)),
                    mint=os.environ.get("CANVAS_MINT", "0") == "1",
                    list_eth=os.environ.get("CANVAS_LIST_PRICE") or os.environ.get("CANVAS_LIST_ETH") or None,
                    list_currency=os.environ.get("CANVAS_LIST_CURRENCY") or ("ETH" if os.environ.get("CANVAS_LIST_ETH") else None),
                    max_sittings=_env_int("CANVAS_SITTINGS", 0) or _env_int("CANVAS_MAX_SUPPLY", 5000))

    app = FastAPI(title="Canvas Fly", docs_url=None, redoc_url=None)
    app.state.painter = painter
    app.state.studio = studio

    # The public site normally reaches this server through Vercel rewrites (same origin).
    # CORS is opened read-only so the pages also work when pointed straight at the studio.
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

    tunnel: Tunnel | None = None
    if os.environ.get("CANVAS_TUNNEL", "0") == "1":
        def _tunnel_ready(url: str) -> None:
            (SITE / "live.json").write_text(json.dumps({"studio": url, "since": time.time()}), encoding="utf-8")
            painter.event("tunnel", f"studio reachable at {url}")
            if os.environ.get("CANVAS_AUTOPUBLISH", "0") == "1":
                threading.Thread(target=studio._publish, args=("the studio address",), daemon=True).start()
        tunnel = Tunnel(_env_int("CANVAS_PORT", 4660), _tunnel_ready, on_event=lambda kind, text: painter.event(kind, text))
    app.state.tunnel = tunnel

    @app.on_event("startup")
    def _start() -> None:
        studio.start()
        if tunnel is not None:
            try:
                tunnel.start()
            except Exception as e:
                painter.event("error", f"tunnel failed: {e}")

    @app.on_event("shutdown")
    def _stop() -> None:
        studio.stop.set()
        if tunnel is not None:
            tunnel.stop()

    @app.get("/api/info")
    def info() -> JSONResponse:
        d = painter.info()
        d["mint_enabled"] = studio.mint
        d["contract"] = os.environ.get("CANVAS_CONTRACT", "")
        d["network"] = os.environ.get("CANVAS_NETWORK", "robinhood")
        d["studio_url"] = tunnel.url if tunnel else None
        return JSONResponse(d)

    @app.get("/api/state")
    def state() -> JSONResponse:
        d = painter.state()
        d["next_sitting_in_s"] = None if studio.next_sitting_at is None else max(0, round(studio.next_sitting_at - time.time()))
        d["mint_enabled"] = studio.mint
        d["resting"] = studio.resting
        d["sold_out"] = studio.sold_out
        d["sittings_done"] = studio.done
        d["sittings_max"] = studio.max_sittings
        d["list_eth"] = studio.list_eth
        d["list_currency"] = studio.list_currency
        d["list_paused_for_s"] = max(0, round(studio._list_paused_until - time.time()))
        d["list_last_error"] = studio.list_last_error
        d["now"] = time.time()
        return JSONResponse(d, headers={"Cache-Control": "no-store"})

    @app.get("/api/frame.png")
    def frame(size: int = 512) -> Response:
        size = max(64, min(1024, size))
        return Response(painter.frame_png(size), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/eye.png")
    def eye() -> Response:
        return Response(painter.eye_png(), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/neurons")
    def neurons() -> JSONResponse:
        return JSONResponse({"xyz": painter.sub_xyz, "cls": painter.sub_class,
                             "legend": ["optic", "central", "vnc", "descending", "kenyon"]},
                            headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/gallery")
    def gallery() -> JSONResponse:
        p = GALLERY / "index.json"
        items = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        items.sort(key=lambda x: x["id"], reverse=True)
        return JSONResponse(items, headers={"Cache-Control": "no-store"})

    @app.get("/api/piece/{piece_id}")
    def piece(piece_id: int) -> JSONResponse:
        p = GALLERY / f"canvas-{piece_id:04d}.json"
        if not p.exists():
            raise HTTPException(404, "no such piece")
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))

    GALLERY.mkdir(exist_ok=True)
    app.mount("/gallery", StaticFiles(directory=GALLERY), name="gallery")
    app.mount("/", StaticFiles(directory=SITE, html=True), name="site")
    return app


def serve(port: int = 4660, host: str = "127.0.0.1", graph_path: Path | None = None, sittings: int | None = None,
          pause_s: float | None = None, autopublish: bool = False, tunnel: bool = False) -> None:
    import uvicorn
    if sittings is not None:
        os.environ["CANVAS_SITTINGS"] = str(sittings)
    if pause_s is not None:
        os.environ["CANVAS_PAUSE_S"] = str(pause_s)
    if autopublish:
        os.environ["CANVAS_AUTOPUBLISH"] = "1"
    if tunnel:
        os.environ["CANVAS_TUNNEL"] = "1"
    os.environ["CANVAS_PORT"] = str(port)
    uvicorn.run(build_app(graph_path), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve(port=_env_int("CANVAS_PORT", 4660))
