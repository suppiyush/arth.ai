"""A supplier agent, run once per config:

    python -m agents.supplier.agent suppliers/shree_traders.yaml

It is an independent business, so it answers any buyer, not only Arth. It takes payment as the
Payment Protocol seller and checks every transaction on-chain before confirming an order.
"""

import asyncio
import sys

from uagents import Context, Protocol
from uagents_core.contrib.protocols.payment import (CommitPayment, CompletePayment, RejectPayment, RequestPayment,
                                                    payment_protocol_spec)

from agents.supplier import logic
from core import chain
from core.models import RFQ, Counter, OrderConfirmed, PlaceOrder, Quote
from core.net import make_agent

supplier = logic.load(sys.argv[1] if len(sys.argv) > 1 else "suppliers/shree_traders.yaml")
agent = make_agent(supplier.agent, supplier.name,
                   description=f"{supplier.name}: building-materials supplier that quotes and negotiates with buyer agents.",
                   # a CommitPayment handler waits on the chain; keep answering quotes meanwhile
                   handle_messages_concurrently=True)
proto = Protocol(name="arth-supplier", version="0.1.0")
payments = Protocol(spec=payment_protocol_spec, role="seller")


@proto.on_message(RFQ, replies=Quote)
async def on_rfq(ctx: Context, sender: str, msg: RFQ):
    quote = logic.on_rfq(supplier, ctx.storage, sender, msg)
    ctx.logger.info(f"RFQ {msg.po_id}: {msg.qty:g} {msg.item} -> "
                    f"{quote.unit_price_paise / 100 if quote.ok else 'no quote'}")
    await ctx.send(sender, quote)


@proto.on_message(Counter, replies=Quote)
async def on_counter(ctx: Context, sender: str, msg: Counter):
    quote = logic.on_counter(supplier, ctx.storage, sender, msg)
    ctx.logger.info(f"Counter {msg.po_id}: asked {msg.target_unit_price_paise / 100}, "
                    f"now {quote.unit_price_paise / 100}")
    await ctx.send(sender, quote)


@proto.on_message(PlaceOrder, replies={RequestPayment, Quote})
async def on_place_order(ctx: Context, sender: str, msg: PlaceOrder):
    reply = logic.on_place_order(supplier, ctx.storage, sender, msg, chain.address(supplier.agent))
    ctx.logger.info(f"PlaceOrder {msg.po_id}: "
                    + (f"requesting {reply.accepted_funds[0].amount} FET" if isinstance(reply, RequestPayment) else "refused"))
    await ctx.send(sender, reply)


@payments.on_message(CommitPayment)
async def on_commit_payment(ctx: Context, sender: str, msg: CommitPayment):
    reply, confirmed = await asyncio.to_thread(logic.on_commit_payment, supplier, ctx.storage, sender, msg,
                                               chain.address(supplier.agent))
    ctx.logger.info(f"CommitPayment {msg.reference} tx {msg.transaction_id[:12]}: "
                    + ("verified" if isinstance(reply, CompletePayment) else f"cancelled ({reply.reason})"))
    await ctx.send(sender, reply)
    if confirmed:
        await ctx.send(sender, confirmed)


@payments.on_message(RejectPayment)
async def on_reject_payment(ctx: Context, sender: str, msg: RejectPayment):
    ctx.logger.info(f"payment rejected: {msg.reason}")


agent.include(proto, publish_manifest=True)
agent.include(payments, publish_manifest=True)


@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info(f"{supplier.name} address: {agent.address}, wallet: {chain.address(supplier.agent)}")


if __name__ == "__main__":
    agent.run()
