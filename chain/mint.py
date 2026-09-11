"""
Mint a finished piece as an ERC-721.

    python run.py mint 12            # dry run: builds the metadata, pins nothing, signs nothing
    python run.py mint 12 --live     # publishes metadata, mints, records tx and token id

Network: Robinhood Chain (id 4663), where gas costs a fraction of a cent and
which OpenSea indexes under the slug `robinhood`. Metadata can live in two places:

    CANVAS_METADATA=site   (default) image + JSON served from the site itself
                           (site/nft/, published with the next `run.py publish`)
    CANVAS_METADATA=ipfs   pinned through Pinata (needs CANVAS_PINATA_JWT)

Either way the PNG's SHA-256 and the sitting's seed go on chain next to the
token via `mint(to, uri, pngSha256, seed)`, so provenance is checkable against
the block, not against a URL.

CANVAS_OWNER is the address that receives the token (default: the painter
wallet). Point it at your own wallet to list by hand on OpenSea.

A piece painted on a surrogate graph is refused: it is not a fly.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from web3 import Web3

from canvas.painter import GALLERY

ROOT = Path(__file__).resolve().parent.parent
SITE_NFT = ROOT / "site" / "nft"

NETWORKS = {
    "robinhood": {
        "chain_id": 4663,
        "rpc": os.environ.get("CANVAS_RPC", "https://rpc.mainnet.chain.robinhood.com"),
        "explorer": "https://robinhoodchain.blockscout.com",
        "opensea": os.environ.get("CANVAS_OPENSEA_BASE", "https://opensea.io/assets/robinhood"),
        "legacy_gas": True,
    },
}

ABI = [
    {"type": "function", "name": "mint", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "uri", "type": "string"},
                {"name": "pngSha256", "type": "bytes32"}, {"name": "seed", "type": "uint64"}],
     "outputs": [{"name": "id", "type": "uint256"}]},
    {"type": "function", "name": "nextId", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "event", "name": "Transfer", "anonymous": False,
     "inputs": [{"indexed": True, "name": "from", "type": "address"},
                {"indexed": True, "name": "to", "type": "address"},
                {"indexed": True, "name": "tokenId", "type": "uint256"}]},
]

SITE_URL = os.environ.get("CANVAS_SITE_URL", "https://connectomecanvas.com").rstrip("/")


def load_piece(piece_id: int) -> tuple[Path, dict]:
    p = GALLERY / f"canvas-{piece_id:04d}.json"
    if not p.exists():
        raise SystemExit(f"no piece #{piece_id} in {GALLERY}")
    return p, json.loads(p.read_text(encoding="utf-8"))


def save_piece(path: Path, piece: dict) -> None:
    path.write_text(json.dumps(piece, indent=1), encoding="utf-8")
    index_path = GALLERY / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    index = [p for p in index if p["id"] != piece["id"]] + [piece]
    index.sort(key=lambda p: p["id"])
    index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    # keep the static recordings index in step so the public gallery shows chain state too
    rec_path = ROOT / "site" / "recordings" / "index.json"
    if rec_path.exists():
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        for r in rec:
            if r["id"] == piece["id"]:
                r["chain"] = piece.get("chain")
        rec_path.write_text(json.dumps(rec, indent=1), encoding="utf-8")


def metadata_for(piece: dict, image_uri: str) -> dict:
    swatches = ", ".join(piece.get("swatches", [])[:6])
    return {
        "name": piece["name"],
        "description": (
            f"Painted by a real fruit-fly nervous system: {piece['neurons']:,} traced neurons of the "
            f"male Drosophila CNS connectome (Janelia FlyEM, Cambridge Connectomics, Google Research, CC-BY 4.0), "
            f"simulated as leaky integrate-and-fire units. The retina looked at the canvas, the descending "
            f"neurons moved the brush, population activity chose the colour. "
            f"{piece['brain_ms'] / 1000:.1f} s of brain time, {piece['strokes']} strokes, {piece['spikes']:,} spikes. "
            f"Seed {piece['seed']} — replayable to the pixel with the same graph. Nobody drew this. "
            f"Watch it being painted: {SITE_URL}/demo#{piece['id']}"
        ),
        "image": image_uri,
        "external_url": f"{SITE_URL}/demo#{piece['id']}",
        "attributes": [
            {"trait_type": "Seed", "value": str(piece["seed"])},
            {"trait_type": "Brain time (s)", "value": round(piece["brain_ms"] / 1000, 2)},
            {"trait_type": "Strokes", "value": piece["strokes"]},
            {"trait_type": "Spikes", "value": piece["spikes"]},
            {"trait_type": "Brush lifts (DNp09)", "value": piece["lifts"]},
            {"trait_type": "Reversals (MDN)", "value": piece["reversals"]},
            {"trait_type": "Synapses changed", "value": piece["synapses_changed"]},
            {"trait_type": "Neurons", "value": piece["neurons"]},
            {"trait_type": "Palette", "value": swatches},
            {"trait_type": "Dataset", "value": piece["dataset"]},
            {"trait_type": "Graph sha256", "value": piece["graph_sha256"]},
            {"trait_type": "PNG sha256", "value": piece["png_sha256"]},
        ],
    }


def publish_metadata(piece: dict, png_path: Path, mode: str) -> tuple[str, str, dict]:
    """Returns (image_uri, metadata_uri, extra) for the chosen metadata mode."""
    if mode == "ipfs":
        from .ipfs import pin_file, pin_json
        image_cid = pin_file(png_path, piece["png"])
        meta = metadata_for(piece, f"ipfs://{image_cid}")
        meta_cid = pin_json(meta, f"{piece['png']}.json")
        return f"ipfs://{image_cid}", f"ipfs://{meta_cid}", {"image_cid": image_cid, "metadata_cid": meta_cid}
    # site: copy the PNG and write the JSON where the static site serves them
    SITE_NFT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(png_path, SITE_NFT / piece["png"])
    image_uri = f"{SITE_URL}/nft/{piece['png']}"
    meta = metadata_for(piece, image_uri)
    (SITE_NFT / f"{piece['id']}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return image_uri, f"{SITE_URL}/nft/{piece['id']}.json", {"metadata_file": f"site/nft/{piece['id']}.json"}


def mint_piece(piece_id: int, dry_run: bool = True, network: str | None = None) -> dict:
    path, piece = load_piece(piece_id)
    if piece.get("surrogate"):
        raise SystemExit("this piece was painted on a surrogate graph, not the connectome; it is not minted")
    if piece.get("chain") and piece["chain"].get("token_id") is not None:
        raise SystemExit(f"piece #{piece_id} is already token {piece['chain']['token_id']}")

    network = network or os.environ.get("CANVAS_NETWORK", "robinhood")
    net = NETWORKS[network]
    mode = os.environ.get("CANVAS_METADATA", "site")
    contract_addr = os.environ.get("CANVAS_CONTRACT", "")
    png_path = GALLERY / piece["png"]

    if dry_run:
        meta = metadata_for(piece, f"{SITE_URL}/nft/{piece['png']}" if mode == "site" else "ipfs://<cid-of-png>")
        return {"dry_run": True, "network": network, "metadata_mode": mode,
                "contract": contract_addr or "<CANVAS_CONTRACT unset>",
                "to": os.environ.get("CANVAS_OWNER") or "<painter wallet>", "png": str(png_path), "metadata": meta}

    if not contract_addr:
        raise SystemExit("CANVAS_CONTRACT is not set")

    from .keystore import load_account
    acct = load_account()
    to = Web3.to_checksum_address(os.environ.get("CANVAS_OWNER") or acct.address)

    image_uri, meta_uri, extra = publish_metadata(piece, png_path, mode)

    w3 = Web3(Web3.HTTPProvider(net["rpc"], request_kwargs={"timeout": 60}))
    if w3.eth.chain_id != net["chain_id"]:
        raise SystemExit(f"rpc is chain {w3.eth.chain_id}, expected {net['chain_id']}")
    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_addr), abi=ABI)
    png_sha = bytes.fromhex(piece["png_sha256"])
    fn = contract.functions.mint(to, meta_uri, png_sha, int(piece["seed"]) & ((1 << 64) - 1))
    tx_params = {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": net["chain_id"]}
    if net["legacy_gas"]:
        tx_params["gasPrice"] = int(w3.eth.gas_price * 1.2)
    else:
        base_fee = w3.eth.get_block("latest").get("baseFeePerGas", w3.to_wei(0.01, "gwei"))
        tip = w3.to_wei(0.001, "gwei")
        tx_params.update({"maxFeePerGas": int(base_fee * 2 + tip), "maxPriorityFeePerGas": tip})
    tx = fn.build_transaction(tx_params)
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=240)
    if receipt.status != 1:
        raise SystemExit(f"mint reverted: {tx_hash.hex()}")
    token_id = None
    for log in contract.events.Transfer().process_receipt(receipt):
        token_id = int(log["args"]["tokenId"])
    result = {
        "network": network,
        "contract": Web3.to_checksum_address(contract_addr),
        "token_id": token_id,
        "tx": tx_hash.hex(),
        "block": receipt.blockNumber,
        "owner": to,
        "metadata_mode": mode,
        "image": image_uri,
        "metadata": meta_uri,
        **extra,
        "explorer": f"{net['explorer']}/tx/{tx_hash.hex()}",
        "opensea": f"{net['opensea']}/{Web3.to_checksum_address(contract_addr)}/{token_id}",
        "minted_at": time.time(),
        "listing": None,
    }
    piece["chain"] = result
    save_piece(path, piece)
    return result
