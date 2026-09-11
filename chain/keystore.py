"""
The painter's wallet.

The private key lives only in an encrypted JSON keystore (the standard Web3
Secret Storage format that MetaMask, geth and ethers all read), by default at
~/.connectome-canvas/keystore.json — outside the repository, never in .env,
never in an environment variable. The passphrase is asked for on the terminal,
or read from a file named by CANVAS_KEYSTORE_PASSWORD_FILE for unattended runs.
Nothing in this module prints or logs a key.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

DEFAULT_PATH = Path.home() / ".connectome-canvas" / "keystore.json"


def keystore_path() -> Path:
    return Path(os.environ.get("CANVAS_KEYSTORE", DEFAULT_PATH)).expanduser()


def _password(confirm: bool = False) -> str:
    pw_file = os.environ.get("CANVAS_KEYSTORE_PASSWORD_FILE")
    if pw_file:
        return Path(pw_file).read_text(encoding="utf-8").strip()
    pw = getpass.getpass("keystore passphrase: ")
    if confirm:
        again = getpass.getpass("again: ")
        if pw != again:
            raise SystemExit("passphrases differ")
    if len(pw) < 8:
        raise SystemExit("use at least 8 characters")
    return pw


def new_keystore(path: Path | None = None) -> str:
    path = path or keystore_path()
    if path.exists():
        raise SystemExit(f"{path} already exists; move it away first")
    path.parent.mkdir(parents=True, exist_ok=True)
    acct = Account.create()
    pw = _password(confirm=True)
    encrypted = Account.encrypt(acct.key, pw)
    path.write_text(json.dumps(encrypted), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"painter address: {acct.address}")
    print(f"keystore:        {path}")
    print("fund it with a little ETH on Robinhood Chain for gas, and set CANVAS_CONTRACT once the contract is deployed.")
    return acct.address


_UNLOCKED: dict[str, LocalAccount] = {}


def load_account(path: Path | None = None) -> LocalAccount:
    """Decrypt once per process (scrypt is deliberately slow); the key never leaves memory."""
    path = path or keystore_path()
    cached = _UNLOCKED.get(str(path))
    if cached is not None:
        return cached
    if not path.exists():
        raise SystemExit(f"no keystore at {path}; run `python run.py wallet new`")
    encrypted = json.loads(path.read_text(encoding="utf-8"))
    acct = Account.from_key(Account.decrypt(encrypted, _password()))
    _UNLOCKED[str(path)] = acct
    return acct


def show_address(path: Path | None = None) -> None:
    path = path or keystore_path()
    if not path.exists():
        raise SystemExit(f"no keystore at {path}")
    encrypted = json.loads(path.read_text(encoding="utf-8"))
    addr = encrypted.get("address", "")
    print(f"0x{addr}" if addr and not addr.startswith("0x") else addr)
