"""Shared uAgent wiring for specialists: Plan -> PlanResult, Commit -> Committed, Query -> QueryResult,
Undo -> UndoDone.

Synchronous logic functions (psycopg) run in a thread; async ones (those calling ASI1) are awaited.
A logic function with a `ctx` parameter also gets the uAgent Context (Procurement uses it to reach suppliers).
Only the orchestrator may call a specialist; anything else is dropped. A crash becomes ok=False,
never silence.
"""

import asyncio
import inspect
from typing import Callable

from uagents import Agent, Context, Protocol

from core import net
from core.models import Commit, Committed, Plan, PlanResult, Query, QueryResult, Undo, UndoDone


async def call(fn: Callable, *args, ctx: Context | None = None):
    kwargs = {"ctx": ctx} if ctx is not None and "ctx" in inspect.signature(fn).parameters else {}
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def serve(agent: Agent, name: str, plan: Callable | None = None, commit: Callable | None = None,
          query: Callable | None = None, undo: Callable | None = None) -> Protocol:
    proto = Protocol(name=f"arth-{name}", version="0.1.0")
    orchestrator = net.address("orchestrator")

    def allowed(ctx: Context, sender: str) -> bool:
        if sender != orchestrator:
            ctx.logger.warning(f"ignoring message from {sender}")
            return False
        return True

    if plan:
        @proto.on_message(Plan, replies=PlanResult)
        async def on_plan(ctx: Context, sender: str, msg: Plan):
            if not allowed(ctx, sender):
                return
            try:
                res = await call(plan, msg.request_id, msg.business_id, msg.intent, msg.fields, ctx=ctx)
            except Exception as err:
                ctx.logger.exception("plan failed")
                res = PlanResult(request_id=msg.request_id, ok=False, error=str(err))
            await ctx.send(sender, res)

    if commit:
        @proto.on_message(Commit, replies=Committed)
        async def on_commit(ctx: Context, sender: str, msg: Commit):
            if not allowed(ctx, sender):
                return
            try:
                res = await call(commit, msg.request_id, msg.action_id, msg.business_id, msg.effects, ctx=ctx)
            except Exception as err:
                ctx.logger.exception("commit failed")
                res = Committed(request_id=msg.request_id, ok=False, message=str(err))
            await ctx.send(sender, res)

    if query:
        @proto.on_message(Query, replies=QueryResult)
        async def on_query(ctx: Context, sender: str, msg: Query):
            if not allowed(ctx, sender):
                return
            try:
                res = await call(query, msg.request_id, msg.business_id, msg.intent, msg.fields, ctx=ctx)
            except Exception as err:
                ctx.logger.exception("query failed")
                res = QueryResult(request_id=msg.request_id, ok=False, text=str(err))
            await ctx.send(sender, res)

    if undo:
        @proto.on_message(Undo, replies=UndoDone)
        async def on_undo(ctx: Context, sender: str, msg: Undo):
            if not allowed(ctx, sender):
                return
            try:
                res = await call(undo, msg.action_id, ctx=ctx)
            except Exception as err:
                ctx.logger.exception("undo failed")
                res = UndoDone(action_id=msg.action_id, ok=False, message=str(err))
            await ctx.send(sender, res)

    agent.include(proto, publish_manifest=True)

    @agent.on_event("startup")
    async def startup(ctx: Context):
        ctx.logger.info(f"{agent.name} address: {agent.address}")

    return proto
