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

    def __init__(self, painter: Painter, pause_s: float = 20.0, mint: bool = False, list_eth: str | None = None):
        self.painter = painter
        self.pause_s = pause_s
        self.mint = mint and not painter.brain.surrogate
        self.list_eth = list_eth
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="studio", daemon=True)
        self.next_sitting_at: float | None = None

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        p = self.painter
        while not self.stop.is_set():
            p.new_session()
            while not self.stop.is_set() and p.tick():
                time.sleep(0)
            if self.stop.is_set():
                break
            piece = p.finished[-1] if p.finished else None
            if piece and self.mint:
                threading.Thread(target=self._mint, args=(piece["id"],), daemon=True).start()
            self.next_sitting_at = time.time() + self.pause_s
            self.stop.wait(self.pause_s)
            self.next_sitting_at = None

    def _mint(self, piece_id: int) -> None:
        try:
            from chain.mint import mint_piece
            self.painter.event("mint", f"minting #{piece_id} …")
            result = mint_piece(piece_id, dry_run=False)
            self.painter.event("mint", f"minted #{piece_id} as token {result['token_id']} · {result['tx'][:12]}…")
            if self.list_eth:
                from chain.opensea import list_piece
                self.painter.event("list", f"listing token {result['token_id']} at {self.list_eth} ETH …")
                listing = list_piece(piece_id, self.list_eth)
                self.painter.event("list", f"listed · {listing.get('url', '')}")
        except Exception as e:  # the studio keeps painting whatever happens on chain
            self.painter.event("error", f"chain step failed for #{piece_id}: {type(e).__name__}: {e}")


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
                    list_eth=os.environ.get("CANVAS_LIST_ETH") or None)

    app = FastAPI(title="Connectome Canvas", docs_url=None, redoc_url=None)
    app.state.painter = painter
    app.state.studio = studio

    @app.on_event("startup")
    def _start() -> None:
        studio.start()

    @app.on_event("shutdown")
    def _stop() -> None:
        studio.stop.set()

    @app.get("/api/info")
    def info() -> JSONResponse:
        d = painter.info()
        d["mint_enabled"] = studio.mint
        d["contract"] = os.environ.get("CANVAS_CONTRACT", "")
        d["network"] = os.environ.get("CANVAS_NETWORK", "base")
        return JSONResponse(d)

    @app.get("/api/state")
    def state() -> JSONResponse:
        d = painter.state()
        d["next_sitting_in_s"] = None if studio.next_sitting_at is None else max(0, round(studio.next_sitting_at - time.time()))
        d["mint_enabled"] = studio.mint
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


def serve(port: int = 4660, host: str = "127.0.0.1", graph_path: Path | None = None) -> None:
    import uvicorn
    uvicorn.run(build_app(graph_path), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve(port=_env_int("CANVAS_PORT", 4660))
