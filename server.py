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
                 max_sittings: int = 0):
        self.painter = painter
        self.pause_s = pause_s
        self.mint = mint and not painter.brain.surrogate
        self.list_eth = list_eth
        self.max_sittings = max_sittings          # 0 = paint forever; N = paint N sittings, then rest
        self.done = 0
        self.resting = False
        self.sold_out = False
        self._chain_lock = threading.Lock()       # mint → publish → list, one piece at a time, in order
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="studio", daemon=True)
        self.next_sitting_at: float | None = None
        self._publish_lock = threading.Lock()     # one deploy at a time; the stage dir is shared

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
        """mint → publish → list, serialised so ids, files and nonces never race."""
        with self._chain_lock:
            if self.mint:
                self._mint(piece_id)
            if os.environ.get("CANVAS_AUTOPUBLISH", "0") == "1":
                self._publish(piece_id)
            if self.mint and self.list_eth:
                self._list_backlog()

    def _publish(self, what) -> None:
        """Push site/ (scores, thumbnails, gallery index, live.json) to Vercel so the public site stays current."""
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
                else:
                    self.painter.event("error", f"publish failed: {(proc.stderr or proc.stdout).strip()[-200:]}")
            except Exception as e:
                self.painter.event("error", f"publish failed: {type(e).__name__}: {e}")

    def _mint(self, piece_id: int) -> None:
        try:
            from chain.mint import mint_piece
            self.painter.event("mint", f"minting #{piece_id} …")
            result = mint_piece(piece_id, dry_run=False)
            self.painter.event("mint", f"minted #{piece_id} as token {result['token_id']} · {result['tx'][:12]}…")
        except SystemExit as e:
            msg = str(e)
            if "sold out" in msg:
                self.sold_out = True
            self.painter.event("error", f"mint failed for #{piece_id}: {msg}")
        except Exception as e:  # the studio keeps painting whatever happens on chain
            self.painter.event("error", f"mint failed for #{piece_id}: {type(e).__name__}: {e}")

    def _list_backlog(self, max_per_pass: int = 3) -> None:
        """List every minted-but-unlisted piece, oldest first, a few per pass; failures are retried next time."""
        import json as _json
        from chain.opensea import list_piece
        index = self.painter._read_index()
        todo = [p for p in index if (p.get("chain") or {}).get("token_id") is not None
                and not (p.get("chain") or {}).get("listing") and (p.get("chain") or {}).get("list_attempts", 0) < 8]
        for p in todo[:max_per_pass]:
            try:
                self.painter.event("list", f"listing #{p['id']} (token {p['chain']['token_id']}) at {self.list_eth} ETH …")
                listing = list_piece(p["id"], self.list_eth)
                self.painter.event("list", f"listed #{p['id']} · {listing.get('url', '')}")
            except BaseException as e:  # SystemExit included: OpenSea may not have indexed the token yet
                gp = GALLERY / f"canvas-{p['id']:04d}.json"
                try:
                    piece = _json.loads(gp.read_text(encoding="utf-8"))
                    piece.setdefault("chain", {})["list_attempts"] = piece["chain"].get("list_attempts", 0) + 1
                    from chain.mint import save_piece
                    save_piece(gp, piece)
                except Exception:
                    pass
                self.painter.event("error", f"listing #{p['id']} failed (will retry): {str(e)[:160]}")


class Tunnel:
    """A Cloudflare quick tunnel in front of the local studio, so the public site can reach the live brain.

    No account and no DNS: cloudflared hands out a random https://….trycloudflare.com address, which
    is written to site/live.json (and published) whenever it changes. The address dies with the process."""

    URL_RE = __import__("re").compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    def __init__(self, port: int, on_ready) -> None:
        self.port = port
        self.on_ready = on_ready
        self.url: str | None = None
        self.proc = None

    def start(self) -> None:
        import shutil
        import subprocess
        exe = shutil.which("cloudflared")
        if not exe:
            candidate = Path.home() / "scoop" / "shims" / "cloudflared.exe"
            exe = str(candidate) if candidate.exists() else None
        if not exe:
            raise RuntimeError("cloudflared not found (scoop install cloudflared)")
        self.proc = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._pump, name="tunnel", daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            m = self.URL_RE.search(line)
            # cloudflared also logs its own API endpoint (api.trycloudflare.com); the tunnel is the other one
            if m and not self.url and not m.group(0).startswith("https://api."):
                self.url = m.group(0)
                try:
                    self.on_ready(self.url)
                except Exception:
                    pass

    def stop(self) -> None:
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
                    list_eth=os.environ.get("CANVAS_LIST_ETH") or None,
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
        tunnel = Tunnel(_env_int("CANVAS_PORT", 4660), _tunnel_ready)
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
