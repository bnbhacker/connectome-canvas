"""
Compile and deploy contracts/ConnectomeCanvas.sol without Foundry.

py-solc-x fetches the compiler, OpenZeppelin comes from tools/node_modules
(`npm install` in tools/), the painter's keystore signs.

    python run.py deploy --network robinhood            # dry run: compiles, estimates gas, prints cost
    python run.py deploy --network robinhood --live     # broadcasts, writes build/deploy-<network>.json
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
SOLC_VERSION = "0.8.26"


_IMPORT_RE = __import__("re").compile(r'import\s+(?:[^;"\']*?\s+from\s+)?["\']([^"\']+)["\']\s*;')
OZ_PREFIX = "@openzeppelin/contracts/"


def _collect_sources(entry: Path, oz: Path) -> dict:
    """Transitive closure of imports, keyed by virtual import path, contents inlined."""
    import posixpath
    sources: dict[str, dict] = {}
    queue: list[tuple[str, Path]] = [("ConnectomeCanvas.sol", entry)]
    while queue:
        name, path = queue.pop()
        if name in sources:
            continue
        text = path.read_text(encoding="utf-8")
        sources[name] = {"content": text}
        for imp in _IMPORT_RE.findall(text):
            if imp.startswith(OZ_PREFIX):
                virt = imp
            elif imp.startswith("."):
                virt = posixpath.normpath(posixpath.join(posixpath.dirname(name), imp))
            else:
                raise SystemExit(f"unknown import {imp!r} in {name}")
            if not virt.startswith(OZ_PREFIX):
                raise SystemExit(f"import {imp!r} in {name} resolves outside OpenZeppelin: {virt}")
            queue.append((virt, oz / virt[len(OZ_PREFIX):]))
    return sources


def compile_contract() -> tuple[list, str]:
    import solcx
    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        print(f"installing solc {SOLC_VERSION} ...")
        solcx.install_solc(SOLC_VERSION)
    oz = ROOT / "tools" / "node_modules" / "@openzeppelin" / "contracts"
    if not oz.exists():
        raise SystemExit("OpenZeppelin not found: run `npm install` in tools/")
    src_path = ROOT / "contracts" / "ConnectomeCanvas.sol"
    # Feed solc every source as content under its import path instead of letting it
    # touch the file system: solc on Windows cannot open files under non-ASCII paths.
    sources = _collect_sources(src_path, oz)
    out = solcx.compile_standard(
        {
            "language": "Solidity",
            "sources": sources,
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "outputSelection": {"ConnectomeCanvas.sol": {"ConnectomeCanvas": ["abi", "evm.bytecode.object"]}},
            },
        },
        solc_version=SOLC_VERSION,
    )
    if "errors" in out:
        for e in out["errors"]:
            if e.get("severity") == "error":
                raise SystemExit(e.get("formattedMessage", e))
    c = out["contracts"]["ConnectomeCanvas.sol"]["ConnectomeCanvas"]
    abi, bytecode = c["abi"], c["evm"]["bytecode"]["object"]
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "ConnectomeCanvas.json").write_text(json.dumps({"abi": abi, "bytecode": bytecode}, indent=1), encoding="utf-8")
    return abi, bytecode


def deploy(network: str, keeper: str | None = None, royalty: str | None = None, dry_run: bool = True) -> dict:
    """keeper = the person's wallet (owner: manages the collection, gets royalties);
    painter = the keystore wallet (may only mint). Both default from CANVAS_OWNER / the keystore."""
    from .keystore import load_account
    from .mint import NETWORKS

    net = NETWORKS[network]
    abi, bytecode = compile_contract()
    print(f"compiled ConnectomeCanvas: {len(bytecode) // 2:,} bytes of bytecode")

    acct = load_account()
    painter = acct.address
    keeper_addr = Web3.to_checksum_address(keeper or os.environ.get("CANVAS_OWNER") or painter)
    royalty_addr = Web3.to_checksum_address(royalty) if royalty else keeper_addr
    w3 = Web3(Web3.HTTPProvider(net["rpc"], request_kwargs={"timeout": 60}))
    if w3.eth.chain_id != net["chain_id"]:
        raise SystemExit(f"rpc is chain {w3.eth.chain_id}, expected {net['chain_id']}")

    max_supply = int(os.environ.get("CANVAS_MAX_SUPPLY", "5000"))
    base_uri = os.environ.get("CANVAS_BASE_URI") or (os.environ.get("CANVAS_SITE_URL", "https://connectomecanvas.com").rstrip("/") + "/nft/")
    print(f"maxSupply {max_supply} · baseURI {base_uri}")
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    gas_price = w3.eth.gas_price
    tx = contract.constructor(keeper_addr, painter, royalty_addr, max_supply, base_uri).build_transaction({
        "from": painter,
        "nonce": w3.eth.get_transaction_count(painter),
        "chainId": net["chain_id"],
        "gasPrice": int(gas_price * 1.2),
    })
    gas = w3.eth.estimate_gas(tx)
    tx["gas"] = int(gas * 1.15)
    cost_eth = tx["gas"] * tx["gasPrice"] / 1e18
    balance = w3.eth.get_balance(painter) / 1e18
    print(f"network {network} (chain {net['chain_id']}) · keeper/owner {keeper_addr} · painter {painter} · royalty → {royalty_addr}")
    print(f"gas ≈ {gas:,} at {tx['gasPrice'] / 1e9:.4f} gwei → ≈ {cost_eth:.6f} ETH · painter balance {balance:.6f} ETH")
    if dry_run:
        return {"dry_run": True, "network": network, "keeper": keeper_addr, "painter": painter, "royalty": royalty_addr,
                "max_supply": max_supply, "base_uri": base_uri, "gas": gas, "cost_eth": cost_eth, "balance_eth": balance}
    if balance < cost_eth:
        raise SystemExit("not enough ETH on the painter wallet for gas")

    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"sent {tx_hash.hex()} — waiting ...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt.status != 1:
        raise SystemExit(f"deploy reverted: {tx_hash.hex()}")
    address = receipt.contractAddress
    result = {
        "network": network, "chain_id": net["chain_id"], "contract": address, "keeper": keeper_addr, "painter": painter,
        "royalty": royalty_addr, "max_supply": max_supply, "base_uri": base_uri, "tx": tx_hash.hex(), "block": receipt.blockNumber,
        "explorer": f"{net['explorer']}/address/{address}", "deployed_at": time.time(),
    }
    (ROOT / "build" / f"deploy-{network}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"ConnectomeCanvas deployed at {address}")
    print(f"export CANVAS_NETWORK={network}  CANVAS_CONTRACT={address}")
    return result
