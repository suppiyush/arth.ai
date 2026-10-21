"""Agent identities, ports and routing.

Arth's own agents talk to each other directly over HTTP on AGENT_HOST (fast, no Agentverse hop);
every other address (ASI:One users, supplier agents elsewhere) is resolved through the Almanac.
Only the orchestrator needs a mailbox.
"""

from uagents import Agent
from uagents.crypto import Identity
from uagents.resolver import GlobalResolver, Resolver

from core import config

PORTS = {
    "orchestrator": 8001, "ledger": 8002, "inventory": 8003, "billing": 8004, "collections": 8005,
    "procurement": 8006, "insights": 8007, "supplier_shree": 8008, "supplier_gupta": 8009, "supplier_balaji": 8010,
}

ASK_TIMEOUT = 10        # seconds per specialist reply (G-9)
ASK_TIMEOUTS = {"procurement": 20}      # a restock plan waits on supplier quotes and a counter round


def timeout_for(name: str) -> int:
    return ASK_TIMEOUTS.get(name, ASK_TIMEOUT)


def address(name: str) -> str:
    return Identity.from_seed(config.seed(name), 0).address


def endpoint(name: str) -> str:
    return f"http://{config.AGENT_HOST}:{PORTS[name]}/submit"


class LocalFirstResolver(Resolver):
    def __init__(self):
        self._local = {address(n): [endpoint(n)] for n in PORTS if _has_seed(n)}
        self._fallback = GlobalResolver()

    async def resolve(self, destination: str) -> tuple[str | None, list[str]]:
        if destination in self._local:
            return destination, self._local[destination]
        return await self._fallback.resolve(destination)


def _has_seed(name: str) -> bool:
    try:
        config.seed(name)
        return True
    except RuntimeError:
        return False


def make_agent(name: str, display_name: str, mailbox: bool = False, **kwargs) -> Agent:
    """Specialists get a local HTTP endpoint; the orchestrator gets a mailbox for ASI:One."""
    return Agent(
        name=display_name,
        seed=config.seed(name),
        port=PORTS[name],
        mailbox=mailbox,
        endpoint=None if mailbox else [endpoint(name)],
        resolve=LocalFirstResolver(),
        **kwargs,
    )
