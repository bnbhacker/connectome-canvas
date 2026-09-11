"""
Verify ConnectomeCanvas on Robinhood Chain's Blockscout from the exact standard-JSON input the
deploy compiled, so the source sits next to the bytecode for anyone to read.

    python -m chain.verify 0xContract
"""
from __future__ import annotations

import json
import sys

import requests

from .deploy import ROOT, SOLC_VERSION, _collect_sources

EXPLORER_API = "https://robinhoodchain.blockscout.com/api/v2"


def standard_input() -> dict:
    oz = ROOT / "tools" / "node_modules" / "@openzeppelin" / "contracts"
    return {
        "language": "Solidity",
        "sources": _collect_sources(ROOT / "contracts" / "ConnectomeCanvas.sol", oz),
        "settings": {"optimizer": {"enabled": True, "runs": 200},
                     "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}}},
    }


def compiler_version() -> str:
    try:
        import solcx
        solcx.set_solc_version(SOLC_VERSION)
        return "v" + str(solcx.get_solc_version(with_commit_hash=True))
    except Exception:
        return "v0.8.26+commit.8a97fa7a"


def verify(address: str) -> dict:
    url = f"{EXPLORER_API}/smart-contracts/{address}/verification/via/standard-input"
    files = {"files[0]": ("input.json", json.dumps(standard_input()), "application/json")}
    data = {"compiler_version": compiler_version(), "contract_name": "ConnectomeCanvas",
            "autodetect_constructor_args": "true", "license_type": "mit"}
    r = requests.post(url, files=files, data=data, timeout=180)
    return {"status": r.status_code, "body": r.text[:500], "compiler": data["compiler_version"]}


SOURCIFY = "https://sourcify.dev/server"


def sourcify(address: str, creation_tx: str, chain_id: int = 4663, poll_s: float = 4.0, tries: int = 40) -> dict:
    """Verify through Sourcify (which supports Robinhood Chain); Blockscout reads Sourcify matches.
    Blockscout's own API sits behind a Cloudflare challenge for scripts, Sourcify does not."""
    import time
    body = {"stdJsonInput": standard_input(), "compilerVersion": compiler_version().removeprefix("v"),
            "contractIdentifier": "ConnectomeCanvas.sol:ConnectomeCanvas", "creationTransactionHash": creation_tx}
    r = requests.post(f"{SOURCIFY}/v2/verify/{chain_id}/{address}", json=body, timeout=180)
    try:
        vid = r.json().get("verificationId")
    except ValueError:
        vid = None
    if not vid:
        return {"status": r.status_code, "body": r.text[:500]}
    for _ in range(tries):
        time.sleep(poll_s)
        s = requests.get(f"{SOURCIFY}/v2/verify/{vid}", timeout=60).json()
        if s.get("isJobCompleted"):
            c = s.get("contract") or {}
            return {"match": c.get("match"), "creationMatch": c.get("creationMatch"),
                    "runtimeMatch": c.get("runtimeMatch"), "error": s.get("error"), "verificationId": vid}
    return {"verificationId": vid, "status": "still running"}


if __name__ == "__main__":
    # python -m chain.verify 0xContract [0xCreationTx]   (with a creation tx: Sourcify, else Blockscout)
    if len(sys.argv) > 2:
        print(json.dumps(sourcify(sys.argv[1], sys.argv[2]), indent=1))
    else:
        print(json.dumps(verify(sys.argv[1]), indent=1))
