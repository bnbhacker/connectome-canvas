"""
List a minted piece on OpenSea.

A listing is a signed Seaport order, and the well-trodden path for that is
OpenSea's own SDK, so this hands off to tools/list.mjs (Node, opensea-js) and
feeds it the keystore passphrase on stdin. Needs OPENSEA_API_KEY.

    cd tools && npm install
    python run.py list 12 --price 0.02
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .keystore import keystore_path, _password
from .mint import load_piece, save_piece

ROOT = Path(__file__).resolve().parent.parent


def list_piece(piece_id: int, price_eth: str, days: int = 30) -> dict:
    path, piece = load_piece(piece_id)
    chain = piece.get("chain") or {}
    if chain.get("token_id") is None:
        raise SystemExit(f"piece #{piece_id} is not minted yet")
    if not os.environ.get("OPENSEA_API_KEY"):
        raise SystemExit("OPENSEA_API_KEY is not set")
    script = ROOT / "tools" / "list.mjs"
    if not (ROOT / "tools" / "node_modules").exists():
        raise SystemExit("run `npm install` in tools/ first")
    cmd = ["node", str(script), "--network", chain["network"], "--contract", chain["contract"],
           "--token", str(chain["token_id"]), "--price", str(price_eth), "--days", str(days),
           "--keystore", str(keystore_path())]
    proc = subprocess.run(cmd, input=_password() + "\n", capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise SystemExit(f"list.mjs failed:\n{proc.stderr.strip()}")
    listing = json.loads(proc.stdout.strip().splitlines()[-1])
    chain["listing"] = {"price_eth": str(price_eth), "days": days, **listing}
    piece["chain"] = chain
    save_piece(path, piece)
    return chain["listing"]
