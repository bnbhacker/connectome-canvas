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


if __name__ == "__main__":
    print(json.dumps(verify(sys.argv[1]), indent=1))
