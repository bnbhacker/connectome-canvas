"""
List a minted piece on OpenSea from the painter wallet — a Seaport 1.6 order,
built and signed here, posted to OpenSea's orderbook.

    python run.py list 12 --price 2.5 --currency USDG             # posts the listing (needs OPENSEA_API_KEY)
    python run.py list 12 --price 0.001 --currency ETH --dry-run  # builds and signs, prints the order, posts nothing

The price is always expressed in the listing currency, and nothing ever converts it.
OpenSea can pin a collection to one currency: this collection answered a native-ETH
listing with "Collection requires currency: 0x5fc5…d168", which is USDG (Global Dollar,
6 decimals). Listing 0.001 "in USDG" would mean a tenth of a cent, so a currency
mismatch stops the listing with a message instead of re-pricing it.

opensea-js does not know Robinhood Chain, so the order is assembled by hand the way
OpenSea's own orders on that chain look (decoded from live fills):
    offer          the ERC-721 (our contract, tokenId)
    consideration  the price, minus OpenSea's 1 % and the collection's creator fee
    zone           OpenSea's signed zone, orderType FULL_RESTRICTED (buyers fulfil through OpenSea)
    conduit        OpenSea's Robinhood Chain conduit, which the painter approves once
The first listing sends one setApprovalForAll transaction (cents on Robinhood Chain);
every listing after that is a signature only. Proceeds arrive in the listing currency;
`run.py sweep` moves them to the keeper.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from decimal import Decimal
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
NATIVE = "0x0000000000000000000000000000000000000000"
# Listing currencies by symbol, per chain. USDG is Robinhood Chain's Global Dollar (6 decimals).
CURRENCIES = {"robinhood": {"ETH": NATIVE, "USDG": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"}}
REQUIRED_CURRENCY_RE = re.compile(r"requires currency:\s*(0x[0-9a-fA-F]{40})")
MISMATCH = "requires this collection to be priced in"

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
ERC20_ABI = [
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"type": "function", "name": "symbol", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
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


def is_native(currency: str) -> bool:
    return int(currency, 16) == 0


def resolve_currency(network: str, name: str | None = None) -> str:
    """'ETH' / 'USDG' / 0x… (default CANVAS_LIST_CURRENCY, then ETH) → the currency's address."""
    name = (name or os.environ.get("CANVAS_LIST_CURRENCY") or "ETH").strip()
    if name.lower().startswith("0x"):
        return Web3.to_checksum_address(name)
    table = CURRENCIES.get(network, {"ETH": NATIVE})
    key = "ETH" if name.upper() == "NATIVE" else name.upper()
    if key not in table:
        raise SystemExit(f"unknown currency {name!r} on {network}; known: {', '.join(table)} or a 0x token address")
    return Web3.to_checksum_address(table[key])


def currency_info(w3: Web3, currency: str) -> tuple[int, str]:
    """(decimals, symbol) of the listing currency."""
    if is_native(currency):
        return 18, "ETH"
    t = w3.eth.contract(address=Web3.to_checksum_address(currency), abi=ERC20_ABI)
    try:
        symbol = t.functions.symbol().call()
    except Exception:
        symbol = "ERC20"
    return int(t.functions.decimals().call()), symbol


def ensure_approval(w3: Web3, acct, contract: str, net: dict) -> str | None:
    nft = w3.eth.contract(address=Web3.to_checksum_address(contract), abi=ERC721_ABI)
    if nft.functions.isApprovedForAll(acct.address, CONDUIT).call():
        return None
    tx = nft.functions.setApprovalForAll(CONDUIT, True).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address, "pending"), "chainId": net["chain_id"],
        "gasPrice": int(w3.eth.gas_price * 1.2),
    })
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=240)
    if rc.status != 1:
        raise SystemExit(f"approval reverted: {h.hex()}")
    return h.hex()


def build_order(w3: Web3, acct, contract: str, token_id: int, price_units: int, days: int,
                fees: list[tuple[float, str]], currency: str = NATIVE) -> dict:
    seaport = w3.eth.contract(address=SEAPORT, abi=SEAPORT_ABI)
    counter = int(seaport.functions.getCounter(acct.address).call())
    now = int(time.time())
    item_type = 0 if is_native(currency) else 1           # 0 = native ETH, 1 = ERC-20
    token = NATIVE if item_type == 0 else Web3.to_checksum_address(currency)
    consideration = []
    fee_total = 0
    for pct, recipient in fees:
        amt = price_units * int(round(pct * 100)) // 10_000
        fee_total += amt
        consideration.append({"itemType": item_type, "token": token, "identifierOrCriteria": 0,
                              "startAmount": amt, "endAmount": amt, "recipient": recipient})
    consideration.insert(0, {"itemType": item_type, "token": token, "identifierOrCriteria": 0,
                             "startAmount": price_units - fee_total, "endAmount": price_units - fee_total,
                             "recipient": acct.address})
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
    nums = ("itemType", "identifierOrCriteria", "startAmount", "endAmount")
    return {
        "offerer": order["offerer"], "zone": order["zone"],
        "offer": [{k: (s(v) if k in nums else v) for k, v in it.items()} for it in order["offer"]],
        "consideration": [{k: (s(v) if k in nums else v) for k, v in it.items()} for it in order["consideration"]],
        "orderType": order["orderType"], "startTime": s(order["startTime"]), "endTime": s(order["endTime"]),
        "zoneHash": order["zoneHash"], "salt": s(order["salt"]), "conduitKey": order["conduitKey"],
        "totalOriginalConsiderationItems": s(len(order["consideration"])), "counter": s(order["counter"]),
    }


def list_piece(piece_id: int, price: str, days: int = 30, dry_run: bool = False, currency: str | None = None) -> dict:
    """List one minted piece at `price`, expressed in `currency` (default CANVAS_LIST_CURRENCY, then ETH)."""
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

    slug = None
    if not dry_run or os.environ.get("OPENSEA_API_KEY"):
        try:
            slug = collection_slug(network, chain["contract"])
        except SystemExit:
            slug = None
    fees = required_fees(slug) if (slug or not dry_run) else [(1.0, OPENSEA_FEE_RECIPIENT)]

    currency = resolve_currency(network, currency)
    decimals, symbol = currency_info(w3, currency)
    price_units = int(Decimal(str(price)) * (Decimal(10) ** decimals))
    if price_units <= 0:
        raise SystemExit(f"price {price} {symbol} rounds to zero")

    approval_tx = None if dry_run else ensure_approval(w3, acct, chain["contract"], net)
    order = build_order(w3, acct, chain["contract"], int(chain["token_id"]), price_units, days, fees, currency)
    signature = sign_order(acct, order, net["chain_id"])
    body = {"parameters": _api_order(order), "signature": signature, "protocol_address": SEAPORT}
    if dry_run:
        return {"dry_run": True, "slug": slug, "fees": fees, "price": str(price), "currency": currency,
                "currency_symbol": symbol, "order": body}

    r = requests.post(f"{API}/orders/{network}/seaport/listings", headers=_headers(), data=json.dumps(body), timeout=60)
    if r.status_code >= 300:
        m = REQUIRED_CURRENCY_RE.search(r.text)
        if m and m.group(1).lower() != currency.lower():
            _, wanted = currency_info(w3, m.group(1))
            raise SystemExit(f"OpenSea {MISMATCH} {wanted} ({m.group(1)}), not {symbol}. The price is never converted "
                             f"automatically: set CANVAS_LIST_CURRENCY={wanted} with a price in {wanted}, or switch the "
                             f"collection's currency on OpenSea.")
        raise SystemExit(f"OpenSea rejected the listing ({r.status_code}): {r.text[:400]}")

    resp = r.json()
    order_hash = (resp.get("order") or {}).get("order_hash") or resp.get("order_hash")
    listing = {
        "price": str(price), "price_eth": str(price) if is_native(currency) else None,
        "currency": currency, "currency_symbol": symbol,
        "days": days, "order_hash": order_hash, "slug": slug,
        "approval_tx": approval_tx, "listed_at": time.time(), "expires": order["endTime"],
        "url": f"{net['opensea']}/{Web3.to_checksum_address(chain['contract'])}/{chain['token_id']}",
    }
    chain["listing"] = listing
    piece["chain"] = chain
    save_piece(path, piece)
    return listing
