import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture
def db(tmp_path):
    from db import DB
    return DB(tmp_path / "t.db")


class FakeSrc:
    """DexScreener-shaped fake with settable numbers (rules-mode tests use fake DexScreener numbers)."""
    def __init__(self, price=1.0, liq=50000.0, **kw):
        self.price, self.liq = price, liq
        self.kw = {"vol_h1": 30000, "buys_h1": 200, "sells_h1": 10, "chg_h1": 5, "chg_m5": 1, "buys_m5": 20, "sells_m5": 5}
        self.kw.update(kw)

    async def dex_pairs(self, mints):
        k = self.kw
        return [{"pairAddress": "POOL", "dexId": "pumpswap", "priceUsd": str(self.price), "liquidity": {"usd": self.liq},
                 "baseToken": {"address": m, "symbol": "TST", "name": "Test"}, "marketCap": 100000,
                 "volume": {"m5": 1000, "h1": k["vol_h1"], "h24": 1}, "txns": {"m5": {"buys": k["buys_m5"], "sells": k["sells_m5"]},
                 "h1": {"buys": k["buys_h1"], "sells": k["sells_h1"]}},
                 "priceChange": {"m5": k["chg_m5"], "h1": k["chg_h1"], "h24": 10}, "pairCreatedAt": 0} for m in mints]


async def no_sleep(*_a, **_k):
    return None
