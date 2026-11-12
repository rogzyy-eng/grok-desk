"""4 test_executor — $200 → $16 → under $47.50 → bump sends $47.50, skip skips; PAPER $200 with skip →
"every coin will be skipped"; pool liquidity $40,000 caps any size at $800. Never places orders."""
import asyncio
import inspect

import fees
from agents import executor as exmod
from agents.executor import Executor, size_ticket, skip_warning
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier


def test_fees_tiers():
    assert fees.side_fee(10) == round(10 * 0.02 * 0.9, 6)
    assert fees.side_fee(47.50) == round(0.95 * 0.9, 6)
    assert fees.side_fee(190) == round(0.95 * 0.9, 6)
    assert fees.side_fee(400) == round(400 * 0.005 * 0.9, 6)


def test_default_ten_dollar_ticket(db):
    c = db.section("executor")
    assert c["min_ticket_usd"] == 10.0
    assert size_ticket(200, c)[0] == 16.0                      # 8% of $200, above $10, no bump
    assert size_ticket(200, c, liq=1000)[0] == 16.0            # 2% of $1,000 = $20 cap
    assert size_ticket(200, c, liq=400)[0] is None             # 2% of $400 = $8 < $10 -> skip
    assert size_ticket(100, c)[0] == 10.0                      # $8 bumped to $10


def test_bump_skip_cap(db):
    db.set("executor.min_ticket_usd", 47.5)
    c = db.section("executor")
    assert round(200 * c["stack_pct"] / 100, 2) == 16.0
    assert size_ticket(200, c)[0] == 47.50
    assert size_ticket(200, {**c, "below_min": "skip"})[0] is None
    assert size_ticket(100000, c, liq=40000)[0] == 800.0
    assert skip_warning(db) is None
    db.set("executor.below_min", "skip")
    assert "every coin will be skipped" in skip_warning(db)


def test_paper_fill_max_open_rechecks(db):
    db.set("executor.min_ticket_usd", 47.5)
    n = Notifier(db, Bus(db))
    ex = Executor(db, Bus(db), FakeSrc(price=0.001, liq=50000), n, sleep=no_sleep)
    ref = {"price_usd": 0.001, "liq_usd": 50000}
    for i in range(3):
        db.insert("coins", {"mint": f"M{i}", "symbol": f"T{i}"})
        asyncio.run(ex.process({"mint": f"M{i}", "pair": ref}))
    assert len(db.open_positions()) == 2 and abs(db.cash() - 105) < 1e-9
    assert db.one("SELECT size_usd FROM signals")["size_usd"] == 47.5
    db2 = db
    ex.src = FakeSrc(price=0.0012, liq=50000)
    db2.x("DELETE FROM positions")
    db2.insert("coins", {"mint": "PX", "symbol": "PX"})
    assert asyncio.run(ex.process({"mint": "PX", "pair": ref})) is None          # +20% > 15% recheck
    ex.src = FakeSrc(price=0.001, liq=30000)
    assert asyncio.run(ex.process({"mint": "PX", "pair": ref})) is None          # liq -40% > 20%


def test_live_signal_only(db):
    db.set("desk.mode", "LIVE")
    db.x("INSERT OR REPLACE INTO settings VALUES('desk.live_stack','1240',0)")
    n = Notifier(db, Bus(db))
    ex = Executor(db, Bus(db), FakeSrc(price=1.0), n, sleep=no_sleep)
    db.insert("coins", {"mint": "L", "symbol": "LV"})
    asyncio.run(ex.process({"mint": "L", "pair": {"price_usd": 1.0, "liq_usd": 50000}}))
    p = db.open_positions()[0]
    assert p["status"] == "pending" and p["qty"] == 0 and p["size_usd"] == round(1240 * 0.08, 2)
    labels = [b["label"] for b in n.sent[-1]["buttons"]]
    assert labels == ["Buy on FOMO", "Bought"]
    src = inspect.getsource(exmod)
    for bad in ("sendTransaction", "private_key", "password", "swap("):
        assert bad not in src
