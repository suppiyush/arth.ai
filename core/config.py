"""Environment loading. Every setting the agents read comes through here."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set; add it to .env (see .env.example)")
    return value


DATABASE_URL = os.getenv("DATABASE_URL", "")
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
ASI1_API_KEY = os.getenv("ASI1_API_KEY", "")
ASI1_BASE_URL = os.getenv("ASI1_BASE_URL", "https://api.asi1.ai/v1")
ASI1_MODEL = os.getenv("ASI1_MODEL", "asi1")
ASI1_ENABLED = _bool("ASI1_ENABLED", True)    # false: offline fallback only (demo replays save ASI1 quota)

FET_USE_TESTNET = _bool("FET_USE_TESTNET", True)
DEMO_INR_PER_FET = int(os.getenv("DEMO_INR_PER_FET", "10000"))
ARTH_FEE_PCT = float(os.getenv("ARTH_FEE_PCT", "1"))

DEMO_BUSINESS_ID = "00000000-0000-4000-8000-000000000001"   # created by db/seed.sql
PRICES_INCLUDE_GST = _bool("PRICES_INCLUDE_GST", True)   # catalogue and spoken rates already contain GST
AGENT_HOST =os.getenv("AGENT_HOST", "127.0.0.1")   # where Arth's own agents reach each other
CLAIM_DEMO_SHOP =_bool("CLAIM_DEMO_SHOP", True)   # first ASI:One sender takes over the seeded shop


def seed(agent: str) -> str:
    """Agent identity seed, e.g. seed("ledger") reads SEED_LEDGER."""
    return require(f"SEED_{agent.upper()}")
