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


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env (never committed) into the environment, without overriding what is set."""
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split(" #", 1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


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
          pause_s=args.pause, autopublish=args.autopublish, tunnel=args.tunnel)


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
    print(json.dumps(list_piece(args.id, args.price, days=args.days, dry_run=args.dry_run, currency=args.currency),
                     indent=1, default=str))


def cmd_sweep(args: argparse.Namespace) -> None:
    """Send the painter's sale proceeds to the keeper: the listing currency (an ERC-20 on Robinhood Chain)
    and any ETH above a gas reserve. Dry run unless --live."""
    import os
    from web3 import Web3
    from chain.keystore import load_account
    from chain.mint import NETWORKS
    from chain.opensea import CURRENCIES, ERC20_ABI, is_native
    net = NETWORKS["robinhood"]
    keeper = Web3.to_checksum_address(args.to or os.environ.get("CANVAS_OWNER", ""))
    acct = load_account()
    w3 = Web3(Web3.HTTPProvider(net["rpc"], request_kwargs={"timeout": 60}))
    gas_price = int(w3.eth.gas_price * 1.2)
    plan = []
    for addr in CURRENCIES.get("robinhood", {}).values():       # every listing currency, e.g. USDG
        if is_native(addr):
            continue
        t = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
        held, sym, dec = t.functions.balanceOf(acct.address).call(), t.functions.symbol().call(), t.functions.decimals().call()
        print(f"painter {acct.address}: {held / 10 ** dec:.6f} {sym}")
        if held > 0:
            plan.append(("token", t, held, sym, dec))
    bal = w3.eth.get_balance(acct.address)
    reserve = Web3.to_wei(str(args.reserve), "ether")
    eth_out = bal - reserve - 21000 * gas_price
    print(f"painter {acct.address}: {bal / 1e18:.6f} ETH, reserve {args.reserve} ETH -> ETH to send {max(0, eth_out) / 1e18:.6f}")
    if eth_out > 0:
        plan.append(("eth", None, eth_out, "ETH", 18))
    if not plan:
        print("nothing to sweep")
        return
    print("to " + keeper + ": " + ", ".join(f"{amt / 10 ** dec:.6f} {sym}" for _, _, amt, sym, dec in plan))
    if not args.live:
        print("dry run; add --live to send")
        return
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    for kind, t, amt, sym, dec in plan:
        if kind == "token":
            tx = t.functions.transfer(keeper, amt).build_transaction(
                {"from": acct.address, "nonce": nonce, "chainId": net["chain_id"], "gasPrice": gas_price})
            tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
        else:
            tx = {"to": keeper, "value": amt, "gas": 21000, "gasPrice": gas_price, "nonce": nonce, "chainId": net["chain_id"]}
        h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        rc = w3.eth.wait_for_transaction_receipt(h, timeout=240)
        print(f"sent {amt / 10 ** dec:.6f} {sym}: {net['explorer']}/tx/{h.hex()} status={rc.status}")
        nonce += 1


def cmd_mint_all(args: argparse.Namespace) -> None:
    """Mint every unminted piece in order: metadata first, then the site, then the chain."""
    from chain.mint import prepare_metadata, mint_backlog, unminted
    pending = unminted()
    print(f"{len(pending)} unminted piece(s)" + (f": sittings #{pending[0]['id']}…#{pending[-1]['id']}" if pending else ""))
    if not pending:
        return
    if not args.live:
        print("dry run; add --live to write metadata, publish the site and mint")
        return
    prepared = prepare_metadata()
    if not prepared:
        print("nothing to mint")
        return
    print(f"metadata written: sitting #{prepared[0][0]} -> token {prepared[0][1]} … sitting #{prepared[-1][0]} -> token {prepared[-1][1]}")
    site = argparse.Namespace(stage=None, scope="bnbhackers-projects")
    if not args.no_publish:
        cmd_publish(site)
    mint_backlog(ids=[pid for pid, _ in prepared],
                 on_result=lambda r: print(f"  token {r['token_id']}  {r['explorer']}", flush=True))
    if not args.no_publish:
        cmd_publish(site)


def cmd_deploy(args: argparse.Namespace) -> None:
    from chain.deploy import deploy
    import json
    print(json.dumps(deploy(args.network, keeper=args.keeper, royalty=args.royalty, dry_run=not args.live), indent=1))


def cmd_publish(args: argparse.Namespace) -> None:
    """Copy site/ to an ASCII path (Vercel chokes on non-ASCII paths on Windows) and deploy it."""
    import json
    import os
    import shutil
    import subprocess
    import tempfile
    # Vercel's CLI silently fails to deploy from paths with non-ASCII characters on Windows,
    # and the user's temp dir may live under such a profile — so pick an ASCII stage.
    if args.stage:
        stage = Path(args.stage)
    else:
        tmp = Path(tempfile.gettempdir())
        stage = (tmp if str(tmp).isascii() else Path("C:/Users/Public" if sys.platform == "win32" else "/tmp")) / "cc-site"
    import time
    link_src = next((p / ".vercel" / "project.json" for p in [stage] + [stage.with_name(f"{stage.name}-{k}") for k in range(2, 6)]
                     if (p / ".vercel" / "project.json").exists()), None)
    saved_link = link_src.read_text(encoding="utf-8") if link_src else None
    # a hung earlier deploy can hold the stage directory: fall through cc-site, cc-site-2, ... rather than wait on it
    chosen = None
    for k in range(1, 6):
        cand = stage if k == 1 else stage.with_name(f"{stage.name}-{k}")
        try:
            if cand.exists():
                shutil.rmtree(cand)
            chosen = cand
            break
        except PermissionError:
            time.sleep(1.0)
    if chosen is None:
        raise SystemExit("every stage directory is locked by an earlier deploy; kill stale `vercel` processes")
    stage = chosen
    shutil.copytree(ROOT / "site", stage)
    if saved_link:                       # keep the project link so --yes never guesses
        (stage / ".vercel").mkdir(parents=True, exist_ok=True)
        (stage / ".vercel" / "project.json").write_text(saved_link, encoding="utf-8")
    print(f"staged site/ -> {stage}")
    cmd = ["vercel", "--prod", "--yes", "--name", "connectome-canvas"]
    if args.scope:
        cmd += ["--scope", args.scope]
    print(" ".join(cmd))
    timeout_s = int(os.environ.get("CANVAS_PUBLISH_TIMEOUT_S", "540"))
    proc = subprocess.Popen(cmd, cwd=stage, shell=(sys.platform == "win32"), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # kill the whole tree (the shell, node, its helpers); a lone .kill() would orphan node
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        raise SystemExit(f"vercel did not finish within {timeout_s} s; killed")
    lines = (out or "").strip().splitlines()
    print("\n".join(lines[-6:]))
    if proc.returncode != 0:
        raise SystemExit(f"vercel exited with {proc.returncode}")
    # with stdout piped, vercel prints its progress on stderr and a JSON "next steps" block last on stdout,
    # so the proof of a deployment (an https URL / "Aliased" / "Ready") can sit anywhere in the output
    ok = any(("https://" in line and "vercel" in line) or "Aliased" in line or "Ready" in line for line in lines)
    if not ok:
        raise SystemExit("vercel produced no deployment URL")


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
    p.add_argument("--autopublish", action="store_true", help="after every sitting, run `publish` so the public site replays it")
    p.add_argument("--tunnel", action="store_true", help="open a Cloudflare quick tunnel and write its address to site/live.json")

    p = sub.add_parser("wallet")
    p.add_argument("action", choices=["new", "address"])

    p = sub.add_parser("mint")
    p.add_argument("id", type=int)
    p.add_argument("--live", action="store_true", help="actually pin and broadcast; default is a dry run")

    p = sub.add_parser("list")
    p.add_argument("id", type=int)
    p.add_argument("--price", required=True, help="in units of --currency")
    p.add_argument("--currency", default=None, help="ETH (native), USDG, or a 0x token; default CANVAS_LIST_CURRENCY, then ETH")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--dry-run", action="store_true", help="build and sign the Seaport order, post nothing")

    p = sub.add_parser("mint-all", help="mint every unminted piece in order (metadata, then the site, then the chain)")
    p.add_argument("--live", action="store_true")
    p.add_argument("--no-publish", action="store_true")

    p = sub.add_parser("sweep", help="send the painter's ETH (sales proceeds) to the keeper")
    p.add_argument("--to", default=None, help="keeper address (default CANVAS_OWNER)")
    p.add_argument("--reserve", default="0.01", help="ETH to leave for gas")
    p.add_argument("--live", action="store_true")

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
         "mint": cmd_mint, "list": cmd_list, "deploy": cmd_deploy, "publish": cmd_publish,
         "sweep": cmd_sweep, "mint-all": cmd_mint_all}[args.cmd](args)


if __name__ == "__main__":
    main()
