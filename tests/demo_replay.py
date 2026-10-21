"""Replays the full demo against live agents and checks every reply. Run it before each recording:

    python -m tests.demo_replay                 # 1 run: starts the Procfile agents, offline (no ASI1 quota)
    python -m tests.demo_replay --runs 10       # the "green 10 runs in a row" check
    python -m tests.demo_replay --asi1          # agents use ASI1 (about 10 requests per run)
    python -m tests.demo_replay --no-pay        # decline the payment instead of paying on testnet
    python -m tests.demo_replay --attach        # use agents you already started (honcho start)

The six demo messages: a sale, a GST bill (with an edit), dues and a reminder, a stock check, a paid
restock, and a profit question. Every run starts from a freshly reset demo shop, so this WIPES the
database in DATABASE_URL (only a local one unless --allow-remote-reset). The paid restock spends
real testnet FET from this client's own wallet; low wallets are topped up from the orchestrator's
wallet (where restock payments land) or the Dorado faucet.
"""

import argparse
import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents.resolver import RulesBasedResolver
from uagents_core.contrib.protocols.chat import (ChatAcknowledgement, ChatMessage, MetadataContent, TextContent,
                                                 chat_protocol_spec)
from uagents_core.contrib.protocols.payment import (CancelPayment, CommitPayment, CompletePayment, RejectPayment,
                                                    RequestPayment, payment_protocol_spec)

from core import chain, config, db, net

STEP_TIMEOUT = 30           # seconds for an ordinary reply (a restock plan waits up to 20 on suppliers)
PAY_TIMEOUT = 180           # owner payment + supplier payment, both confirmed on-chain
GRACE = 0.4                 # after a step's checks pass, catch any trailing message before moving on
MIN_FET = 25 * 10**17       # keep 2.5 FET in the paying wallets
LOG_DIR = Path(tempfile.gettempdir()) / "arth-replay"

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
ORCH = net.address("orchestrator")
client = Agent(name="arth-demo-replay", seed="arth-demo-replay-local-only", port=8098,
               endpoint=["http://127.0.0.1:8098/submit"],
               resolve=RulesBasedResolver({ORCH: net.endpoint("orchestrator")}))


# Steps

@dataclass
class Seen:
    """Everything received for the current step."""
    texts: list[str] = field(default_factory=list)
    cards: list[tuple[str, dict]] = field(default_factory=list)     # (card_kind, payload)
    requests: list[RequestPayment] = field(default_factory=list)
    other: list[str] = field(default_factory=list)                  # CompletePayment, CancelPayment

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


@dataclass
class Step:
    send: str | Callable[["Replay"], str]           # chat text, or a function picking a card selection
    expect: list[str] = field(default_factory=list) # substrings of the reply text
    card: str | None = None                         # a card of this kind must arrive
    payment_request: bool = False
    action: str = "chat"                            # chat | pay | reject
    timeout: float = STEP_TIMEOUT

    def label(self) -> str:
        return self.send if isinstance(self.send, str) else self.send.__doc__ or "card click"


def selections(payload) -> list[dict]:
    """Every selection in a card payload, wherever it sits (CTAs, carousel items, custom buttons)."""
    found = []
    if isinstance(payload, dict):
        if isinstance(payload.get("selection"), dict):
            found.append(payload["selection"])
        for v in payload.values():
            found += selections(v)
    elif isinstance(payload, list):
        for v in payload:
            found += selections(v)
    return found


def approve(r: "Replay") -> str:
    """click the card's approve / primary button"""
    kind, payload = r.last_card
    cta = payload.get("approve_cta") or (payload.get("ctas") or [None])[0]
    return json.dumps(cta["selection"])


def best_quote(r: "Replay") -> str:
    """choose the best-value supplier"""
    return json.dumps(r.last_card[1]["items"][0]["primary_cta"]["selection"])


def remind_verma(r: "Replay") -> str:
    """click Remind on Verma Constructions"""
    sel = next(s for s in selections(r.last_card[1]) if s.get("action") == "remind"
               and s.get("party") == "Verma Constructions")
    return json.dumps(sel)


def steps(pay: bool) -> list[Step]:
    restock_end = ([Step("@pay", ["✅ Payment mil gaya", "📦 Order confirmed: 50 bag Cement"], action="pay",
                         timeout=PAY_TIMEOUT),
                    Step("Cement kitna bacha hai?", ["Cement: 52 bag bacha hai"])] if pay else
                   [Step("@reject", ["order bhi cancel"], action="reject")])
    return [
        # 1. sale with half paid
        Step("Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila", ["Sale to Ramesh Kumar", "₹2,100"], "review"),
        Step(approve, ["✅ Ho gaya!", "Cement kam hai"]),
        # 2. GST bill, edited, issued
        Step("Bill bana do Verma Constructions ke liye, 200 kg TMT 12mm", ["IGST", "TMT Bar 12mm"], "detail"),
        Step("5% discount", ["Discount: 5%"], "detail"),
        Step(approve, ["Invoice ARTH/", "/0001"], "detail"),
        # 3. dues and a reminder
        Step("Kis kis ka udhaar baaki hai?", ["Verma Constructions", "Ramesh Kumar"], "custom"),
        Step(remind_verma, ["Reminder for Verma Constructions"], "detail"),
        Step(approve, ["wa.me/"]),
        # 4. stock
        Step("Cement kitna bacha hai?", ["Cement: 2 bag bacha hai"]),
        # 5. restock: quotes, choice, approval, payment
        Step("50 bag cement mangwa do", ["Quotes for 50 bag Cement", "mol-bhav"], "carousel"),
        Step(best_quote, ["Restock: 50 bag Cement from Gupta Cement"], "review"),
        Step(approve, ["Approve FET Payment"], payment_request=True),
        *restock_end,
        # 6. profit question
        Step("Is mahine kitna profit hua?", ["Is mahine ka hisaab", "Gross profit"], "detail"),
    ]


# Talking to the orchestrator

inbox: asyncio.Queue = asyncio.Queue()
chat = Protocol(spec=chat_protocol_spec)
payments = Protocol(spec=payment_protocol_spec, role="buyer")


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    await inbox.put(msg)


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


@payments.on_message(RequestPayment)
async def on_request(ctx: Context, sender: str, msg: RequestPayment):
    await inbox.put(msg)


@payments.on_message(CompletePayment)
async def on_complete(ctx: Context, sender: str, msg: CompletePayment):
    await inbox.put(msg)


@payments.on_message(CancelPayment)
async def on_cancel(ctx: Context, sender: str, msg: CancelPayment):
    await inbox.put(msg)


client.include(chat)
client.include(payments)


class Replay:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.last_card: tuple[str, dict] | None = None
        self.last_request: RequestPayment | None = None

    def take(self, msg, seen: Seen) -> None:
        if isinstance(msg, ChatMessage):
            for c in msg.content:
                if isinstance(c, TextContent):
                    seen.texts.append(c.text)
                elif isinstance(c, MetadataContent) and "card_kind" in c.metadata:
                    card = (c.metadata["card_kind"], json.loads(c.metadata["card_payload"]))
                    seen.cards.append(card)
                    self.last_card = card
        elif isinstance(msg, RequestPayment):
            seen.requests.append(msg)
            self.last_request = msg
        else:
            seen.other.append(f"{type(msg).__name__}: {getattr(msg, 'reason', '') or ''}")

    def done(self, step: Step, seen: Seen) -> bool:
        return (all(e in seen.text for e in step.expect)
                and (step.card is None or any(k == step.card for k, _ in seen.cards))
                and (not step.payment_request or seen.requests))

    async def pay(self) -> None:
        req = self.last_request
        funds = req.accepted_funds[0]
        tx = await asyncio.to_thread(chain.client().send_tokens, req.recipient, chain.to_atestfet(funds.amount),
                                     chain.DENOM, client.wallet)
        await asyncio.to_thread(tx.wait_to_complete)
        await self.ctx.send(ORCH, CommitPayment(funds=funds, recipient=req.recipient, transaction_id=tx.tx_hash,
                                                reference=req.reference,
                                                metadata={"buyer_fet_wallet": str(client.wallet.address())}))

    async def run_step(self, step: Step) -> tuple[bool, Seen, float]:
        while not inbox.empty():                    # stragglers from the previous step
            inbox.get_nowait()
        started = time.monotonic()
        if step.action == "pay":
            await self.pay()
        elif step.action == "reject":
            await self.ctx.send(ORCH, RejectPayment(reason="demo replay declines"))
        else:
            body = step.send if isinstance(step.send, str) else step.send(self)
            await self.ctx.send(ORCH, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                                  content=[TextContent(type="text", text=body)]))
        seen = Seen()
        deadline = started + step.timeout
        while not self.done(step, seen):
            try:
                self.take(await asyncio.wait_for(inbox.get(), max(0.1, deadline - time.monotonic())), seen)
            except asyncio.TimeoutError:
                return False, seen, time.monotonic() - started
        elapsed = time.monotonic() - started
        try:
            while True:
                self.take(await asyncio.wait_for(inbox.get(), GRACE), seen)
        except asyncio.TimeoutError:
            pass
        return True, seen, elapsed


# Agents, database and wallets

def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((config.AGENT_HOST, port)) == 0


def start_agents(asi1: bool) -> list[subprocess.Popen]:
    busy = [p for p in net.PORTS.values() if port_open(p)]
    if busy:
        sys.exit(f"Ports {busy} are in use. Stop the running agents, or pass --attach to use them.")
    LOG_DIR.mkdir(exist_ok=True)
    env = {**os.environ, "ASI1_ENABLED": "true" if asi1 else "false", "PYTHONUTF8": "1"}
    procs = []
    for line in (config.ROOT / "Procfile").read_text().splitlines():
        if ":" not in line:
            continue
        name, cmd = line.split(":", 1)
        args = shlex.split(cmd)
        args[0] = sys.executable if args[0] == "python" else args[0]
        log = open(LOG_DIR / f"{name.strip()}.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen(args, cwd=config.ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not all(port_open(p) for p in net.PORTS.values()):
        time.sleep(0.5)
    missing = [n for n, p in net.PORTS.items() if not port_open(p)]
    if missing:
        stop_agents(procs)
        sys.exit(f"Agents didn't start: {missing}. Logs in {LOG_DIR}")
    return procs


def stop_agents(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if sys.platform == "win32":            # the venv launcher spawns the real interpreter as a child
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.terminate()


def check_database(allow_remote: bool) -> None:
    host = urlparse(config.DATABASE_URL).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1") and not allow_remote:
        sys.exit(f"DATABASE_URL points at {host}; each run wipes and re-seeds it. "
                 "Pass --allow-remote-reset if that is the demo database.")


def top_up(addr: str, label: str) -> None:
    """Keep a paying wallet above MIN_FET: from the orchestrator's wallet if it can spare it, else the faucet."""
    have = chain.balance(addr)
    if have >= MIN_FET:
        return
    need = 2 * MIN_FET - have
    orch = chain.address("orchestrator")
    if orch != addr and chain.balance(orch) >= need + MIN_FET:
        chain.send("orchestrator", addr, need)
        print(f"  topped up {label} with {chain.fet_amount(need)} FET from the orchestrator wallet")
        return
    from cosmpy.aerial.faucet import FaucetApi
    FaucetApi(chain.client().network_config).get_wealth(addr)
    print(f"  topped up {label} from the Dorado faucet")


# Main

def show_failure(step: Step, seen: Seen) -> None:
    print(f"    ✗ step {step.label()!r}: expected {step.expect}" + (f" + a {step.card} card" if step.card else "")
          + (" + a payment request" if step.payment_request else ""))
    print("      got: " + (seen.text.replace("\n", "\n           ")[:1500] or "(nothing)"))
    if seen.cards:
        print(f"      cards: {[k for k, _ in seen.cards]}")
    if seen.other:
        print(f"      also: {seen.other}")


async def main(ctx: Context, args, procs: list[subprocess.Popen]) -> None:
    await asyncio.sleep(3)                      # let this client's Almanac registration land so replies can find it
    replay, results = Replay(ctx), []
    try:
        for n in range(1, args.runs + 1):
            if not args.no_pay:
                await asyncio.to_thread(top_up, str(client.wallet.address()), "replay wallet")
                await asyncio.to_thread(top_up, chain.address("procurement"), "procurement wallet")
            await asyncio.to_thread(db.reset, config.DATABASE_URL)
            started, ok, slowest = time.monotonic(), True, ("", 0.0)
            for step in steps(pay=not args.no_pay):
                passed, seen, took = await replay.run_step(step)
                if took > slowest[1] and step.action == "chat":
                    slowest = (step.label(), took)
                if not passed:
                    show_failure(step, seen)
                    ok = False
                    break
            results.append(ok)
            print(f"run {n}/{args.runs} {'✓' if ok else '✗'} {time.monotonic() - started:.0f}s"
                  f" (slowest chat step {slowest[1]:.1f}s: {str(slowest[0])[:40]!r})")
            if not ok and args.stop_on_failure:
                break
    finally:
        stop_agents(procs)
    passed = sum(results)
    print(f"\n{passed}/{len(results)} runs green" + ("" if passed == len(results) else f"; agent logs in {LOG_DIR}"))
    os._exit(0 if results and passed == len(results) else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--asi1", action="store_true", help="let the agents call ASI1 (uses quota)")
    parser.add_argument("--no-pay", action="store_true", help="decline the payment instead of paying on testnet")
    parser.add_argument("--attach", action="store_true", help="use agents that are already running")
    parser.add_argument("--allow-remote-reset", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args()

    check_database(args.allow_remote_reset)
    procs = [] if args.attach else start_agents(args.asi1)
    mode = "attached" if args.attach else ("ASI1" if args.asi1 else "offline (ASI1 off)")
    print(f"Demo replay: {args.runs} run(s), agents {mode}, payment {'declined' if args.no_pay else 'on testnet'}")

    @client.on_event("startup")
    async def start(ctx: Context):
        asyncio.create_task(main(ctx, args, procs))

    try:
        client.run()
    finally:
        stop_agents(procs)
