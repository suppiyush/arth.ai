"""Arth orchestrator: the only agent ASI:One talks to.

    python -m agents.orchestrator.agent

Copy the inspector URL from the startup log, open it, and choose Connect -> Mailbox.
"""

import asyncio
from datetime import datetime, timezone

from uagents import Context, Model, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec
from uagents_core.contrib.protocols.payment import CommitPayment, RejectPayment, payment_protocol_spec

from agents.orchestrator import handler
from core import chain, config, net
from core.models import OrderResult

agent = net.make_agent(
    "orchestrator", "Arth",
    mailbox=True,
    publish_agent_details=True,
    readme_path=str(config.ROOT / "agents" / "orchestrator" / "README.md"),
    description="AI accountant for Indian small shops: khata, GST bills, stock, dues and restocking, in Hinglish.",
    # Handlers await specialist replies; sequential handling would deadlock on those.
    handle_messages_concurrently=True,
)

chat_proto = Protocol(spec=chat_protocol_spec)
# Towards the owner, Arth sells: it requests the restock payment and verifies it.
payment_proto = Protocol(spec=payment_protocol_spec, role="seller")
orders_proto = Protocol(name="arth-orchestrator-orders", version="0.1.0")


class AgentIO:
    """What the handler needs from the uAgent context."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.storage = ctx.storage

    async def ask(self, name: str, msg: Model, response_type: type[Model]) -> Model | None:
        reply, status = await self.ctx.send_and_receive(net.address(name), msg, response_type=response_type,
                                                        timeout=net.timeout_for(name))
        if reply is None:
            self.ctx.logger.warning(f"no reply from {name}: {status.detail}")
        return reply

    async def tell(self, name: str, msg: Model) -> None:
        await self.ctx.send(net.address(name), msg)

    async def send(self, address: str, msg: Model) -> None:
        await self.ctx.send(address, msg)


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = "\n".join(c.text for c in msg.content if isinstance(c, TextContent))
    for reply in await handler.respond(sender, str(msg.msg_id), text, AgentIO(ctx)):
        await ctx.send(sender, reply)


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


@payment_proto.on_message(CommitPayment)
async def on_commit_payment(ctx: Context, sender: str, msg: CommitPayment):
    ctx.logger.info(f"CommitPayment from {sender[:16]}...: tx {msg.transaction_id}")
    await handler.on_commit_payment(sender, msg, AgentIO(ctx))


@payment_proto.on_message(RejectPayment)
async def on_reject_payment(ctx: Context, sender: str, msg: RejectPayment):
    ctx.logger.info(f"RejectPayment from {sender[:16]}...: {msg.reason}")
    await handler.on_reject_payment(sender, msg, AgentIO(ctx))


@orders_proto.on_message(OrderResult)
async def on_order_result(ctx: Context, sender: str, msg: OrderResult):
    if sender != net.address("procurement"):
        ctx.logger.warning(f"ignoring OrderResult from {sender}")
        return
    await handler.on_order_result(msg, AgentIO(ctx))


agent.include(chat_proto, publish_manifest=True)
agent.include(payment_proto, publish_manifest=True)
agent.include(orders_proto)


@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info(f"Arth orchestrator address: {agent.address}")
    addr = chain.address("orchestrator")
    try:
        ctx.logger.info(f"wallet {addr}: {chain.fet_amount(await asyncio.to_thread(chain.balance, addr))} FET")
    except Exception as err:
        ctx.logger.warning(f"couldn't read wallet balance: {err}")


if __name__ == "__main__":
    agent.run()
