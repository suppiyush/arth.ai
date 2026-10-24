"""The 62-message extraction set against real ASI1 (tests/extraction_eval.py).

Opt-in, because one run spends 62 ASI1 requests and the key allows about 100 per window:
    ARTH_LIVE_ASI1=1 python -m pytest tests/test_extraction_live.py
"""

import asyncio
import os

import pytest

from core import config
from tests.extraction_eval import CASES, TARGET, report, run_all

pytestmark = pytest.mark.skipif(not (config.ASI1_API_KEY and os.getenv("ARTH_LIVE_ASI1") == "1"),
                                reason="live ASI1 run is opt-in: set ARTH_LIVE_ASI1=1 (uses 62 requests)")


def test_extraction_set_meets_target():
    assert len(CASES) == 62
    results = asyncio.run(run_all())
    accuracy = sum(r.ok for r in results) / len(results)
    print(report(results))
    assert accuracy >= TARGET, report(results)
