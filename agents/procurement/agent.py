"""Arth Procurement: quotes, negotiation and paying suppliers. python -m agents.procurement.agent

It is the Payment Protocol buyer towards supplier agents, paying from its own testnet wallet.
"""

import asyncio

from uagents import Context, Model, Protocol
from uagents_core.contrib.protocols.payment import (CancelPayment, CommitPayment, CompletePayment, RequestPayment,
                                                    payment_protocol_spec)

from agents.procurement import logic
from core import chain, net
from core.models import OrderConfirmed, OrderResult, PayOrder
from core.net import make_agent
from core.specialist import serve

agent = make_agent("procurement", "Arth Procurement",
                   description="Procurement for Arth: asks supplier agents for quotes, negotiates one round and places orders.",
                   # handlers wait on supplier replies and the chain; let other messages through meanwhile
                   handle_messages_concurrently=True)

confirmations: dict[str, asyncio.Future] = {}       # po_id -> OrderConfirmed expected from a supplier


class ContextSuppliers:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def ask(self, address: str, msg: Model, response_type, timeout: float) -> Model | None:
        reply, status = await self.ctx.send_and_receive(address, msg, response_type=response_type, timeout=int(timeout))
        if reply is None:
            self.ctx.logger.warning(f"no reply to {type(msg).__name__} from {address[:16]}...: {status.detail}")
        return reply

    async def commit_payment(self, address: str, msg: CommitPayment, timeout: float):
        waiter = confirmations[msg.reference] = asyncio.get_running_loop().create_future()
        try:
            done = await self.ask(address, msg, {CompletePayment, CancelPayment}, timeout)
            if not isinstance(done, CompletePayment):
                return done, None
            try:
                return done, await asyncio.wait_for(waiter, 15)
            except asyncio.TimeoutError:
                return done, None
        finally:
            confirmations.pop(msg.reference, None)


async def plan(request_id: str, business_id: str, intent: str, fields: dict, ctx: Context):
    return await logic.plan(request_id, business_id, intent, fields, ContextSuppliers(ctx))


serve(agent, "procurement", plan=plan, commit=logic.commit, undo=logic.undo)

orders = Protocol(name="arth-procurement-orders", version="0.1.0")


@orders.on_message(PayOrder, replies=OrderResult)
async def on_pay_order(ctx: Context, sender: str, msg: PayOrder):
    if sender != net.address("orchestrator"):
        ctx.logger.warning(f"ignoring PayOrder from {sender}")
        return
    try:
        res = await logic.pay_order(msg.request_id, msg.business_id, msg.po_id, msg.user_tx, msg.buyer_wallet,
                                    int(msg.paid_atestfet), ContextSuppliers(ctx))
    except Exception as err:
        ctx.logger.exception("pay_order failed")
        res = OrderResult(request_id=msg.request_id, po_id=msg.po_id, ok=False, message=str(err))
    ctx.logger.info(f"order {msg.po_id}: {'confirmed' if res.ok else 'not completed'} {res.message}")
    await ctx.send(sender, res)


@orders.on_message(OrderConfirmed)
async def on_order_confirmed(ctx: Context, sender: str, msg: OrderConfirmed):
    waiter = confirmations.get(msg.po_id)
    if waiter and not waiter.done():
        waiter.set_result(msg)


agent.include(orders)

# Payment Protocol, buyer role. Replies we wait for arrive through send_and_receive; these handlers
# only see unsolicited messages.
payments = Protocol(spec=payment_protocol_spec, role="buyer")


@payments.on_message(RequestPayment)
async def on_request_payment(ctx: Context, sender: str, msg: RequestPayment):
    ctx.logger.warning(f"unsolicited payment request from {sender[:16]}... ignored")


@payments.on_message(CompletePayment)
async def on_complete_payment(ctx: Context, sender: str, msg: CompletePayment):
    ctx.logger.info(f"late CompletePayment for {msg.transaction_id}")


@payments.on_message(CancelPayment)
async def on_cancel_payment(ctx: Context, sender: str, msg: CancelPayment):
    ctx.logger.info(f"late CancelPayment for {msg.transaction_id}: {msg.reason}")


agent.include(payments, publish_manifest=True)


@agent.on_event("startup")
async def wallet_check(ctx: Context):
    addr = chain.address("procurement")
    try:
        bal = await asyncio.to_thread(chain.balance, addr)
        ctx.logger.info(f"wallet {addr}: {chain.fet_amount(bal)} FET")
        if bal < 10**18:
            ctx.logger.warning("procurement wallet below 1 FET: top it up from the Dorado faucet before the demo")
    except Exception as err:
        ctx.logger.warning(f"couldn't read wallet balance: {err}")


if __name__ == "__main__":
    agent.run()
