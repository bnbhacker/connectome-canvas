"""
Pinning through Pinata. The JWT is an API credential, not a wallet key; it is
read from CANVAS_PINATA_JWT and used for nothing else.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

PIN_FILE = "https://api.pinata.cloud/pinning/pinFileToIPFS"
PIN_JSON = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
GATEWAY = os.environ.get("CANVAS_IPFS_GATEWAY", "https://gateway.pinata.cloud/ipfs/")


def _headers() -> dict:
    jwt = os.environ.get("CANVAS_PINATA_JWT")
    if not jwt:
        raise SystemExit("CANVAS_PINATA_JWT is not set; get a free key at pinata.cloud")
    return {"Authorization": f"Bearer {jwt}"}


def pin_file(path: Path, name: str) -> str:
    with open(path, "rb") as f:
        r = requests.post(PIN_FILE, headers=_headers(),
                          files={"file": (path.name, f, "image/png")},
                          data={"pinataMetadata": json.dumps({"name": name})}, timeout=120)
    r.raise_for_status()
    return r.json()["IpfsHash"]


def pin_json(obj: dict, name: str) -> str:
    r = requests.post(PIN_JSON, headers={**_headers(), "Content-Type": "application/json"},
                      json={"pinataContent": obj, "pinataMetadata": {"name": name}}, timeout=60)
    r.raise_for_status()
    return r.json()["IpfsHash"]


def gateway_url(cid: str) -> str:
    return GATEWAY + cid
