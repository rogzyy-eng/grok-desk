"""Momentum cap: skip a coin already up more than analyst.max_m5_change_pct in 5 min (zirve kovalama)."""
import asyncio

import config
import fields
from agents.analyst import Analyst, m5_cap_reason
from agents.executor import Executor
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier


def test_default_range_and_seed(db):
    assert db.get("analyst.max_m5_change_pct") == 100
    assert config.RANGES["analyst.max_m5_change_pct"] == (20, 1000, 5)
    db.set("analyst.max_m5_change_pct", 150)
    db.seed_settings()                                   # INSERT OR IGNORE keeps Osman's value
    assert db.get("analyst.max_m5_change_pct") == 150


def test_reason_text():
    assert m5_cap_reason(190, 100) == "zirve kovalama: 5dk +%190 > %100"
    assert m5_cap_reason(100, 100) is None and m5_cap_reason(40, 100) is None and m5_cap_reason(None, 100) is None


def _pair(mint, m5):
    return fields.best_pair(asyncio.run(FakeSrc(chg_m5=m5).dex_pairs([mint])))


def test_analyst_rejects_above_cap(db):
    a = Analyst(db, Bus(db), FakeSrc(), Notifier(db, Bus(db)), clock=lambda: 1000.0, sleep=no_sleep)
    db.insert("coins", {"mint": "HOT", "symbol": "HOT"})
    out = asyncio.run(a.judge("HOT", _pair("HOT", 234), {}))
    assert not out["passed"] and out["failed"][0] == "zirve kovalama: 5dk +%234 > %100"
    out = asyncio.run(a.judge("HOT", _pair("HOT", 20), {}))
    assert not any(f.startswith("zirve") for f in out["failed"])
    db.set("analyst.max_m5_change_pct", 300)
    out = asyncio.run(a.judge("HOT", _pair("HOT", 234), {}))
    assert not any(f.startswith("zirve") for f in out["failed"])


def test_executor_rechecks_cap_on_fresh_pair(db):
    db.insert("coins", {"mint": "RUN", "symbol": "RUN"})
    ex = Executor(db, Bus(db), FakeSrc(chg_m5=190), Notifier(db, Bus(db)), clock=lambda: 1000.0, sleep=no_sleep)
    assert asyncio.run(ex.process({"mint": "RUN", "pair": {"price_usd": 1.0, "liq_usd": 50000}})) is None
    assert db.one("SELECT status_reason FROM coins WHERE mint='RUN'")["status_reason"] == "zirve kovalama: 5dk +%190 > %100"
    assert not db.q("SELECT 1 FROM positions WHERE mint='RUN'")
