"""Scripted chat client: talks to the running orchestrator like ASI:One would.

    python -m tests.chat_client "help" "Ramesh ko 10 bag cement becha 4200 mein, half paid cash" @confirm

Each argument is sent in turn after the previous replies settle. "@confirm" / "@cancel" click the
approve / reject button of the last review or detail card (sent as JSON, like a direct @mention selection);
"@choose:2" clicks the second item of the last carousel. "@pay" plays ASI:One's Approve FET Payment:
it pays the last RequestPayment from this client's own testnet wallet (fund it from the faucet) and
sends CommitPayment; "@reject" sends RejectPayment.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents.resolver import RulesBasedResolver
from uagents_core.contrib.protocols.chat import (ChatAcknowledgement, ChatMessage, MetadataContent, TextContent,
                                                 chat_protocol_spec)
from uagents_core.contrib.protocols.payment import (CancelPayment, CommitPayment, CompletePayment, RejectPayment,
                                                    RequestPayment, payment_protocol_spec)

from core import chain, net

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

SETTLE_SECONDS = 4          # reply stream is considered done after this much quiet
PAY_SETTLE_SECONDS = 90     # after @pay, the supplier leg runs on-chain before the last message
REPLY_TIMEOUT = 40

ORCH = net.address("orchestrator")
client = Agent(name="arth-test-client", seed="arth-test-client-local-only", port=8099,
               endpoint=["http://127.0.0.1:8099/submit"],
               resolve=RulesBasedResolver({ORCH: net.endpoint("orchestrator")}))
proto = Protocol(spec=chat_protocol_spec)
payments = Protocol(spec=payment_protocol_spec, role="buyer")
inbox: asyncio.Queue = asyncio.Queue()
last_review: dict = {}
last_carousel: list = []
last_request: list = []


def show(msg: ChatMessage):
    for c in msg.content:
        if isinstance(c, TextContent):
            print("  " + c.text.replace("\n", "\n  "))
        elif isinstance(c, MetadataContent) and "card_kind" in c.metadata:
            payload = json.loads(c.metadata["card_payload"])
            print(f"  [card: {c.metadata['card_kind']}]")
            if c.metadata["card_kind"] == "carousel":
                last_carousel[:] = payload["items"]
                for n, it in enumerate(payload["items"], 1):
                    badges = ", ".join(b["label"] for b in it.get("badges", []))
                    print(f"  [{n}] {it['title']}: {it['subtitle']} | {it.get('secondary_text', '')}"
                          + (f" [{badges}]" if badges else ""))
                continue
            if c.metadata["card_kind"] == "review":
                last_review.update(approve_cta=payload["approve_cta"], reject_cta=payload["reject_cta"])
            elif c.metadata["card_kind"] == "detail" and len(payload.get("ctas", [])) == 2:
                last_review.update(approve_cta=payload["ctas"][0], reject_cta=payload["ctas"][1])
            else:
                continue
            print(f"  [buttons: {last_review['approve_cta']['label']} / {last_review['reject_cta']['label']}]")


@proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    await inbox.put(msg)


@proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


@payments.on_message(RequestPayment)
async def on_request(ctx: Context, sender: str, msg: RequestPayment):
    last_request[:] = [msg]
    f = msg.accepted_funds[0]
    print(f"  [payment request: {f.amount} {f.currency} to {msg.recipient} — {msg.description}]")
    await inbox.put(msg)


@payments.on_message(CompletePayment)
async def on_complete(ctx: Context, sender: str, msg: CompletePayment):
    print(f"  [CompletePayment {msg.transaction_id}]")
    await inbox.put(msg)


@payments.on_message(CancelPayment)
async def on_cancel(ctx: Context, sender: str, msg: CancelPayment):
    print(f"  [CancelPayment: {msg.reason}]")
    await inbox.put(msg)


client.include(proto)
client.include(payments)


async def pay(ctx: Context) -> None:
    req = last_request[0]
    funds = req.accepted_funds[0]
    tx = await asyncio.to_thread(chain.client().send_tokens, req.recipient, chain.to_atestfet(funds.amount),
                                 chain.DENOM, client.wallet)
    await asyncio.to_thread(tx.wait_to_complete)
    print(f"  [paid {funds.amount} FET from {client.wallet.address()}: {tx.tx_hash}]")
    await ctx.send(ORCH, CommitPayment(funds=funds, recipient=req.recipient, transaction_id=tx.tx_hash,
                                       reference=req.reference, metadata={"buyer_fet_wallet": str(client.wallet.address())}))


async def converse(ctx: Context, script: list[str]):
    await asyncio.sleep(3)          # let the client's Almanac registration land so replies can find it
    for line in script:
        if line in ("@confirm", "@cancel"):
            cta = last_review.get("approve_cta" if line == "@confirm" else "reject_cta")
            text = json.dumps(cta["selection"]) if cta else line[1:]
        elif line.startswith("@choose:"):
            text = json.dumps(last_carousel[int(line.split(":")[1]) - 1]["primary_cta"]["selection"])
        else:
            text = line
        print(f"\n> {text}")
        started = asyncio.get_running_loop().time()
        settle = SETTLE_SECONDS
        if line == "@pay":
            settle = PAY_SETTLE_SECONDS
            await pay(ctx)
        elif line == "@reject":
            await ctx.send(ORCH, RejectPayment(reason="owner declined"))
        else:
            await ctx.send(ORCH, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                             content=[TextContent(type="text", text=text)]))
        got, finished = 0, False
        while True:
            try:
                msg = await asyncio.wait_for(inbox.get(), settle if got else REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                break
            got += 1
            if isinstance(msg, ChatMessage):
                show(msg)
                if any(isinstance(c, TextContent) and c.text.startswith(("📦", "❌")) for c in msg.content):
                    finished = True
                    break
        elapsed = asyncio.get_running_loop().time() - started - (settle if got and not finished else 0)
        print(f"  ({got} repl{'y' if got == 1 else 'ies'}, ~{elapsed:.1f}s)" if got else "  (NO REPLY)")
    sys.stdout.flush()
    os._exit(0)     # skip uAgents' noisy shutdown of a throwaway client


@client.on_event("startup")
async def start(ctx: Context):
    asyncio.create_task(converse(ctx, sys.argv[1:] or ["help"]))


if __name__ == "__main__":
    client.run()
