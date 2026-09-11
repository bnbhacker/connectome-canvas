"""
List a minted piece on OpenSea from the painter wallet — a Seaport 1.6 order,
built and signed here, posted to OpenSea's orderbook.

    python run.py list 12 --price 0.005            # posts the listing (needs OPENSEA_API_KEY)
    python run.py list 12 --price 0.005 --dry-run  # builds and signs, prints the order, posts nothing

opensea-js does not know Robinhood Chain, so the order is assembled by hand the way
OpenSea's own orders on that chain look (decoded from live fills):
    offer          the ERC-721 (our contract, tokenId)
    consideration  ETH to the painter, minus OpenSea's 1 % and the collection's creator fee
    zone           OpenSea's signed zone, orderType FULL_RESTRICTED (buyers fulfil through OpenSea)
    conduit        OpenSea's Robinhood Chain conduit, which the painter approves once
The first listing sends one setApprovalForAll transaction (cents on Robinhood Chain);
every listing after that is a signature only.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

import requests
from web3 import Web3

from .mint import NETWORKS, load_piece, save_piece

ROOT = Path(__file__).resolve().parent.parent

# Read off live OpenSea fills on Robinhood Chain (11 Sep 2026): OpenSea's own orders there use
# this conduit, this signed zone, orderType 2 (FULL_RESTRICTED), a 1 % fee to OpenSea and the
# collection's creator fee. Every value can be overridden from the environment if OpenSea moves.
SEAPORT = Web3.to_checksum_address(os.environ.get("CANVAS_SEAPORT", "0x0000000000000068F116a894984e2DB1123eB395"))   # Seaport 1.6
CONDUIT_KEY = os.environ.get("CANVAS_CONDUIT_KEY", "0x61159fefdfada89302ed55f8b9e89e2d67d8258712b3a3f89aa88525877f1d5e")
CONDUIT = Web3.to_checksum_address(os.environ.get("CANVAS_CONDUIT", "0x963F00d3ff000064fFCbA824b800c0000000C300"))
ZONE = Web3.to_checksum_address(os.environ.get("CANVAS_ZONE", "0x000056F7000000EcE9003ca63978907a00FFD100"))          # OpenSea signed zone
ORDER_TYPE = int(os.environ.get("CANVAS_ORDER_TYPE", "2"))                                                             # FULL_RESTRICTED
OPENSEA_FEE_RECIPIENT = Web3.to_checksum_address("0x0000a26b00c1F0DF003000390027140000fAa719")
API = "https://api.opensea.io/api/v2"

SEAPORT_ABI = [
    {"type": "function", "name": "getCounter", "stateMutability": "view",
     "inputs": [{"name": "offerer", "type": "address"}], "outputs": [{"name": "counter", "type": "uint256"}]},
]
ERC721_ABI = [
    {"type": "function", "name": "isApprovedForAll", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "setApprovalForAll", "stateMutability": "nonpayable",
     "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}], "outputs": []},
    {"type": "function", "name": "ownerOf", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]},
]

TYPES = {
    "OrderComponents": [
        {"name": "offerer", "type": "address"}, {"name": "zone", "type": "address"},
        {"name": "offer", "type": "OfferItem[]"}, {"name": "consideration", "type": "ConsiderationItem[]"},
        {"name": "orderType", "type": "uint8"}, {"name": "startTime", "type": "uint256"},
        {"name": "endTime", "type": "uint256"}, {"name": "zoneHash", "type": "bytes32"},
        {"name": "salt", "type": "uint256"}, {"name": "conduitKey", "type": "bytes32"},
        {"name": "counter", "type": "uint256"},
    ],
    "OfferItem": [
        {"name": "itemType", "type": "uint8"}, {"name": "token", "type": "address"},
        {"name": "identifierOrCriteria", "type": "uint256"}, {"name": "startAmount", "type": "uint256"},
        {"name": "endAmount", "type": "uint256"},
    ],
    "ConsiderationItem": [
        {"name": "itemType", "type": "uint8"}, {"name": "token", "type": "address"},
        {"name": "identifierOrCriteria", "type": "uint256"}, {"name": "startAmount", "type": "uint256"},
        {"name": "endAmount", "type": "uint256"}, {"name": "recipient", "type": "address"},
    ],
}


def _headers() -> dict:
    key = os.environ.get("OPENSEA_API_KEY")
    if not key:
        raise SystemExit("OPENSEA_API_KEY is not set (free at docs.opensea.io → Get an API key)")
    return {"X-API-KEY": key, "accept": "application/json", "content-type": "application/json",
            "User-Agent": "connectome-canvas/1.0"}


def collection_slug(chain: str, contract: str) -> str | None:
    r = requests.get(f"{API}/chain/{chain}/contract/{contract}", headers=_headers(), timeout=30)
    if r.status_code != 200:
        return None
    return r.json().get("collection")


def required_fees(slug: str | None) -> list[tuple[float, str]]:
    """[(percent, recipient)] the order must pay out. From OpenSea's collection record when it has one;
    otherwise OpenSea's 1 % plus the contract's 5 % royalty to the keeper."""
    if slug:
        r = requests.get(f"{API}/collections/{slug}", headers=_headers(), timeout=30)
        if r.status_code == 200:
            fees = [(float(f["fee"]), Web3.to_checksum_address(f["recipient"])) for f in r.json().get("fees", []) if f.get("fee")]
            if fees:
                return fees
    fees = [(float(os.environ.get("CANVAS_OPENSEA_FEE_PCT", "1.0")), OPENSEA_FEE_RECIPIENT)]
    keeper = os.environ.get("CANVAS_OWNER")
    if keeper:
        fees.append((float(os.environ.get("CANVAS_ROYALTY_PCT", "5.0")), Web3.to_checksum_address(keeper)))
    return fees


def ensure_approval(w3: Web3, acct, contract: str, net: dict) -> str | None:
    nft = w3.eth.contract(address=Web3.to_checksum_address(contract), abi=ERC721_ABI)
    if nft.functions.isApprovedForAll(acct.address, CONDUIT).call():
        return None
    tx = nft.functions.setApprovalForAll(CONDUIT, True).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": net["chain_id"],
        "gasPrice": int(w3.eth.gas_price * 1.2),
    })
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=240)
    if rc.status != 1:
        raise SystemExit(f"approval reverted: {h.hex()}")
    return h.hex()


def build_order(w3: Web3, acct, contract: str, token_id: int, price_wei: int, days: int,
                fees: list[tuple[float, str]]) -> dict:
    seaport = w3.eth.contract(address=SEAPORT, abi=SEAPORT_ABI)
    counter = int(seaport.functions.getCounter(acct.address).call())
    now = int(time.time())
    consideration = []
    fee_total = 0
    for pct, recipient in fees:
        amt = price_wei * int(round(pct * 100)) // 10_000
        fee_total += amt
        consideration.append({"itemType": 0, "token": "0x0000000000000000000000000000000000000000", "identifierOrCriteria": 0,
                              "startAmount": amt, "endAmount": amt, "recipient": recipient})
    consideration.insert(0, {"itemType": 0, "token": "0x0000000000000000000000000000000000000000", "identifierOrCriteria": 0,
                             "startAmount": price_wei - fee_total, "endAmount": price_wei - fee_total, "recipient": acct.address})
    return {
        "offerer": acct.address,
        "zone": ZONE,
        "offer": [{"itemType": 2, "token": Web3.to_checksum_address(contract), "identifierOrCriteria": int(token_id),
                   "startAmount": 1, "endAmount": 1}],
        "consideration": consideration,
        "orderType": ORDER_TYPE,
        "startTime": now - 120,
        "endTime": now + days * 86400,
        "zoneHash": "0x" + "00" * 32,
        "salt": secrets.randbits(256),
        "conduitKey": CONDUIT_KEY,
        "counter": counter,
    }


def sign_order(acct, order: dict, chain_id: int) -> str:
    domain = {"name": "Seaport", "version": "1.6", "chainId": chain_id, "verifyingContract": SEAPORT}
    signed = acct.sign_typed_data(domain_data=domain, message_types=TYPES, message_data=order)
    return "0x" + signed.signature.hex().removeprefix("0x")


def _api_order(order: dict) -> dict:
    """Seaport order as OpenSea's API wants it: every number a string, plus totalOriginalConsiderationItems."""
    def s(x):
        return str(x)
    return {
        "offerer": order["offerer"], "zone": order["zone"],
        "offer": [{k: (s(v) if k in ("itemType", "identifierOrCriteria", "startAmount", "endAmount") else v) for k, v in it.items()} for it in order["offer"]],
        "consideration": [{k: (s(v) if k in ("itemType", "identifierOrCriteria", "startAmount", "endAmount") else v) for k, v in it.items()} for it in order["consideration"]],
        "orderType": order["orderType"], "startTime": s(order["startTime"]), "endTime": s(order["endTime"]),
        "zoneHash": order["zoneHash"], "salt": s(order["salt"]), "conduitKey": order["conduitKey"],
        "totalOriginalConsiderationItems": s(len(order["consideration"])), "counter": s(order["counter"]),
    }


def list_piece(piece_id: int, price_eth: str, days: int = 30, dry_run: bool = False) -> dict:
    path, piece = load_piece(piece_id)
    chain = piece.get("chain") or {}
    if chain.get("token_id") is None:
        raise SystemExit(f"piece #{piece_id} is not minted yet")
    network = chain.get("network", "robinhood")
    net = NETWORKS[network]

    from .keystore import load_account
    acct = load_account()
    w3 = Web3(Web3.HTTPProvider(net["rpc"], request_kwargs={"timeout": 60}))
    if w3.eth.chain_id != net["chain_id"]:
        raise SystemExit(f"rpc is chain {w3.eth.chain_id}, expected {net['chain_id']}")
    if w3.eth.get_code(SEAPORT) in (b"", b"\x00"):
        raise SystemExit("Seaport 1.6 is not deployed on this chain")

    nft = w3.eth.contract(address=Web3.to_checksum_address(chain["contract"]), abi=ERC721_ABI)
    owner = nft.functions.ownerOf(int(chain["token_id"])).call()
    if owner.lower() != acct.address.lower():
        raise SystemExit(f"token {chain['token_id']} is owned by {owner}, not by the painter; list it from that wallet")

    price_wei = Web3.to_wei(str(price_eth), "ether")
    slug = None
    if not dry_run or os.environ.get("OPENSEA_API_KEY"):
        try:
            slug = collection_slug(network, chain["contract"])
        except SystemExit:
            slug = None
    fees = required_fees(slug) if (slug or not dry_run) else [(1.0, OPENSEA_FEE_RECIPIENT)]

    approval_tx = None if dry_run else ensure_approval(w3, acct, chain["contract"], net)
    order = build_order(w3, acct, chain["contract"], int(chain["token_id"]), price_wei, days, fees)
    signature = sign_order(acct, order, net["chain_id"])
    body = {"parameters": _api_order(order), "signature": signature, "protocol_address": SEAPORT}
    if dry_run:
        return {"dry_run": True, "slug": slug, "fees": fees, "order": body}

    r = requests.post(f"{API}/orders/{network}/seaport/listings", headers=_headers(), data=json.dumps(body), timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"OpenSea rejected the listing ({r.status_code}): {r.text[:400]}")
    resp = r.json()
    order_hash = (resp.get("order") or {}).get("order_hash") or resp.get("order_hash")
    listing = {
        "price_eth": str(price_eth), "days": days, "order_hash": order_hash, "slug": slug,
        "approval_tx": approval_tx, "listed_at": time.time(), "expires": order["endTime"],
        "url": f"{net['opensea']}/{Web3.to_checksum_address(chain['contract'])}/{chain['token_id']}",
    }
    chain["listing"] = listing
    piece["chain"] = chain
    save_piece(path, piece)
    return listing
