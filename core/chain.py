"""Fetch.ai ledger access for the Payment Protocol: agent wallets, sending FET, and verifying that a
transaction really moved the expected amount from the buyer to us.

Verification decodes the transaction's MsgSend messages instead of reading the merged event list,
which also contains the fee transfer.
"""

import time
from decimal import Decimal
from functools import lru_cache

from cosmpy.aerial.client import LedgerClient, NetworkConfig
from cosmpy.aerial.wallet import LocalWallet, PrivateKey
from cosmpy.protos.cosmos.bank.v1beta1.tx_pb2 import MsgSend
from cosmpy.protos.cosmos.tx.v1beta1.service_pb2 import GetTxRequest
from uagents.config import LEDGER_PREFIX
from uagents_core.identity import derive_key_from_seed

from core import config
from core.money import ATTO

DENOM = "atestfet" if config.FET_USE_TESTNET else "afet"
NETWORK_NAME = "stable-testnet" if config.FET_USE_TESTNET else "mainnet"
EXPLORER = "https://explore-dorado.fetch.ai/transactions/" if config.FET_USE_TESTNET else "https://www.mintscan.io/fetchai/tx/"
VERIFY_WAIT = 40            # seconds to wait for a fresh transaction to be indexed


@lru_cache(maxsize=1)
def client() -> LedgerClient:
    return LedgerClient(NetworkConfig.fetchai_stable_testnet() if config.FET_USE_TESTNET
                        else NetworkConfig.fetchai_mainnet())


@lru_cache(maxsize=None)
def wallet(agent: str) -> LocalWallet:
    """The same wallet uAgents derives for Agent(seed=...)."""
    return LocalWallet(PrivateKey(derive_key_from_seed(config.seed(agent), LEDGER_PREFIX, 0)))


def address(agent: str) -> str:
    return str(wallet(agent).address())


def fet_amount(atestfet: int) -> str:
    """Funds.amount is a decimal FET string: 1905365000000000000 -> '1.905365'."""
    return format((Decimal(atestfet) / ATTO).normalize(), "f")


def to_atestfet(amount: str) -> int:
    return int(Decimal(amount) * ATTO)


def explorer(tx_hash: str) -> str:
    return EXPLORER + tx_hash


def balance(addr: str) -> int:
    return int(client().query_bank_balance(addr, DENOM))


def send(agent: str, to: str, atestfet: int) -> str:
    """Transfer from an agent's wallet and wait for it to land. Returns the tx hash."""
    tx = client().send_tokens(to, atestfet, DENOM, wallet(agent))
    tx.wait_to_complete()
    return tx.tx_hash


def transferred(tx_hash: str, sender: str, recipient: str) -> int:
    """atestfet that tx_hash moved from sender to recipient (0 if failed). Raises if not found yet."""
    resp = client().txs.GetTx(GetTxRequest(hash=tx_hash))
    if resp.tx_response.code != 0:
        return 0
    total = 0
    for packed in resp.tx.body.messages:
        if packed.type_url != "/cosmos.bank.v1beta1.MsgSend":
            continue
        msg = MsgSend()
        packed.Unpack(msg)
        if msg.from_address == sender and msg.to_address == recipient:
            total += sum(int(c.amount) for c in msg.amount if c.denom == DENOM)
    return total


def verify_transfer(tx_hash: str, sender: str, recipient: str, min_atestfet: int, wait: float = VERIFY_WAIT) -> str | None:
    """None if tx_hash paid at least min_atestfet from sender to recipient, else the reason it didn't."""
    deadline = time.monotonic() + wait
    while True:
        try:
            amount = transferred(tx_hash, sender, recipient)
            break
        except Exception as err:                    # not indexed yet, or a bad hash
            if time.monotonic() > deadline:
                return f"transaction not found ({type(err).__name__})"
            time.sleep(2)
    if amount < min_atestfet:
        return f"expected {fet_amount(min_atestfet)} FET from {sender[:12]}…, found {fet_amount(amount)} FET"
    return None
