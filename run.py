"""
Connectome Canvas — command line.

    python run.py fetch                 download the connectome (~540 MB, CC-BY, no key)
    python run.py describe              print the columns the files actually have
    python run.py build                 data/*.feather -> build/graph.npz  (the real brain)
    python run.py surrogate             a labelled random stand-in graph for development
    python run.py paint [--ticks N] [--seed S]     one headless sitting -> gallery/ + site/recordings/
    python run.py replay ID             re-run a finished piece from its seed and compare hashes
    python run.py serve [--port 4660]   the studio: sittings back to back + the site
    python run.py wallet new            create the painter's encrypted keystore (outside the repo)
    python run.py deploy [--live]       compile + deploy the contract (Robinhood Chain by default)
    python run.py mint ID [--live]      publish metadata + mint a finished piece (dry run unless --live)
    python run.py publish               push site/ (gallery, recordings, nft metadata) to Vercel
    python run.py list ID --price 0.02  optional: create an OpenSea listing from the CLI
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def cmd_paint(args: argparse.Namespace) -> None:
    from brain.sim import Brain, resolve_graph
    from canvas.painter import Painter
    graph_path = resolve_graph(args.graph)
    brain = Brain(graph_path)
    gains = ROOT / "assets" / "gains.npz"
    painter = Painter(brain, tick_ms=args.tick_ms, budget_ticks=args.ticks, graph_path=graph_path,
                      gains_path=gains if gains.exists() else None)
    info = painter.info()
    print(f"{info['dataset']}: {info['neurons']:,} neurons, {info['edges']:,} edges"
          + ("  [SURROGATE — not a fly]" if info["surrogate"] else ""))
    print(f"retina {info['retina']}  motor {info['motor']}")
    s = painter.new_session(args.seed)
    import time
    t0 = time.time()
    while painter.tick():
        if s.ticks % 50 == 0:
            st = painter.state()
            w = st["window"]
            print(f"  tick {s.ticks:4d}/{args.ticks}  spikes/window {w['spikes']:6d}  firing {w['firing']:6d}  "
                  f"strokes {s.strokes:3d}  colour {s.colour}  {w['wall_ms']} ms/window")
    piece = painter.finished[-1]
    print(f"finished #{piece['id']} in {time.time() - t0:.1f}s: {piece['strokes']} strokes, {piece['spikes']:,} spikes, "
          f"png sha {piece['png_sha256'][:16]}…  -> gallery/{piece['png']}")


def cmd_replay(args: argparse.Namespace) -> None:
    import json
    from brain.sim import Brain, resolve_graph
    from canvas.painter import Painter, GALLERY
    src = json.loads((GALLERY / f"canvas-{args.id:04d}.json").read_text(encoding="utf-8"))
    graph_path = resolve_graph(None)
    brain = Brain(graph_path)
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    painter = Painter(brain, tick_ms=src["brain_ms"] / src["ticks"], budget_ticks=src["ticks"],
                      graph_path=graph_path, gallery=tmp, recordings=tmp)
    if painter.graph_sha != src["graph_sha256"]:
        print(f"graph differs: {painter.graph_sha[:12]} vs {src['graph_sha256'][:12]} — replay cannot match")
    painter.new_session(src["seed"])
    while painter.tick():
        pass
    out = painter.finished[-1]
    same = out["png_sha256"] == src["png_sha256"]
    print(f"original {src['png_sha256'][:16]}…  replay {out['png_sha256'][:16]}…  -> {'MATCH' if same else 'DIFFERENT'}")
    print(f"replay png: {tmp / out['png']}")


def cmd_serve(args: argparse.Namespace) -> None:
    from server import serve
    serve(port=args.port, host=args.host, graph_path=Path(args.graph) if args.graph else None, sittings=args.sittings,
          pause_s=args.pause)


def cmd_wallet(args: argparse.Namespace) -> None:
    from chain.keystore import new_keystore, show_address
    if args.action == "new":
        new_keystore()
    else:
        show_address()


def cmd_mint(args: argparse.Namespace) -> None:
    from chain.mint import mint_piece
    result = mint_piece(args.id, dry_run=not args.live)
    import json
    print(json.dumps(result, indent=1))


def cmd_list(args: argparse.Namespace) -> None:
    from chain.opensea import list_piece
    import json
    print(json.dumps(list_piece(args.id, args.price, days=args.days), indent=1))


def cmd_deploy(args: argparse.Namespace) -> None:
    from chain.deploy import deploy
    import json
    print(json.dumps(deploy(args.network, keeper=args.keeper, royalty=args.royalty, dry_run=not args.live), indent=1))


def cmd_publish(args: argparse.Namespace) -> None:
    """Copy site/ to an ASCII path (Vercel chokes on non-ASCII paths on Windows) and deploy it."""
    import shutil
    import subprocess
    import tempfile
    stage = Path(args.stage) if args.stage else Path(tempfile.gettempdir()) / "cc-site"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(ROOT / "site", stage)
    print(f"staged site/ -> {stage}")
    cmd = ["vercel", "--prod", "--yes", "--name", "connectome-canvas"]
    if args.scope:
        cmd += ["--scope", args.scope]
    print(" ".join(cmd))
    subprocess.run(cmd, cwd=stage, shell=(sys.platform == "win32"), check=False)


def main() -> None:
    ap = argparse.ArgumentParser(prog="run.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch")
    sub.add_parser("describe")
    sub.add_parser("build")
    sub.add_parser("surrogate")

    p = sub.add_parser("paint")
    p.add_argument("--ticks", type=int, default=600)
    p.add_argument("--tick-ms", type=float, default=10.0)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--graph", default=None)

    p = sub.add_parser("replay")
    p.add_argument("id", type=int)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=4660)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--graph", default=None)
    p.add_argument("--sittings", type=int, default=None, help="paint this many sittings, then rest (default: forever)")
    p.add_argument("--pause", type=float, default=None, help="seconds between sittings (default: CANVAS_PAUSE_S or 20)")

    p = sub.add_parser("wallet")
    p.add_argument("action", choices=["new", "address"])

    p = sub.add_parser("mint")
    p.add_argument("id", type=int)
    p.add_argument("--live", action="store_true", help="actually pin and broadcast; default is a dry run")

    p = sub.add_parser("list")
    p.add_argument("id", type=int)
    p.add_argument("--price", required=True, help="ETH")
    p.add_argument("--days", type=int, default=30)

    p = sub.add_parser("deploy", help="compile with py-solc-x and deploy ConnectomeCanvas.sol")
    p.add_argument("--network", default="robinhood", choices=["robinhood"])
    p.add_argument("--keeper", default=None, help="owner wallet: manages the collection, gets royalties (default: CANVAS_OWNER)")
    p.add_argument("--royalty", default=None, help="royalty receiver (default: the keeper)")
    p.add_argument("--live", action="store_true", help="broadcast; default is a dry run with a gas estimate")

    p = sub.add_parser("publish", help="deploy site/ to Vercel from an ASCII staging path")
    p.add_argument("--stage", default=None)
    p.add_argument("--scope", default="bnbhackers-projects")

    args = ap.parse_args()
    if args.cmd in ("fetch", "describe", "build"):
        from brain import graph
        getattr(graph, args.cmd)()
    elif args.cmd == "surrogate":
        from brain.surrogate import build
        print(f"wrote {build()} (surrogate, not a fly)")
    else:
        {"paint": cmd_paint, "replay": cmd_replay, "serve": cmd_serve, "wallet": cmd_wallet,
         "mint": cmd_mint, "list": cmd_list, "deploy": cmd_deploy, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    main()
