"""4 test_executor — $200 → $16 → under $47.50 → bump sends $47.50, skip skips; PAPER $200 with skip →
"every coin will be skipped"; pool liquidity $40,000 caps any size at $800. Never places orders."""
import asyncio
import inspect

import config
import fees
from agents import executor as exmod
from agents.executor import Executor, size_ticket, skip_warning
from agents.risk import Risk
from agents.scanner import Scanner
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier
from sources import FetchError


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


def test_ranges_allow_fast_settings():
    assert config.RANGES["scanner.poll_s"] == (5, 300, 5)
    assert config.RANGES["executor.confirm_min"] == (0, 60, 1)
    assert config.RANGES["risk.poll_s"][0] == 5
    assert config.RANGES["executor.max_open"] == (1, 10, 1)


def test_confirm_min_zero_paper_enters_immediately(db):
    db.set("executor.confirm_min", 0)
    n = Notifier(db, Bus(db))
    t = [1000.0]
    ex = Executor(db, Bus(db), FakeSrc(price=1.0, liq=50000), n, clock=lambda: t[0], sleep=no_sleep)
    db.insert("coins", {"mint": "Z0", "symbol": "Z0"})
    pid = asyncio.run(ex.process({"mint": "Z0", "pair": {"price_usd": 1.0, "liq_usd": 50000}}))
    p = db.one("SELECT * FROM positions WHERE id=?", (pid,))
    assert p["status"] == "open" and p["qty"] > 0
    t[0] += 3600
    asyncio.run(ex.expire_pending())
    assert db.one("SELECT status FROM positions WHERE id=?", (pid,))["status"] == "open"


def test_confirm_min_zero_live_never_auto_expires(db):
    db.set("desk.mode", "LIVE")
    db.x("INSERT OR REPLACE INTO settings VALUES('desk.live_stack','1240',0)")
    db.set("executor.confirm_min", 0)
    n = Notifier(db, Bus(db))
    t = [1000.0]
    ex = Executor(db, Bus(db), FakeSrc(price=1.0, liq=50000), n, clock=lambda: t[0], sleep=no_sleep)
    db.insert("coins", {"mint": "L0", "symbol": "L0"})
    asyncio.run(ex.process({"mint": "L0", "pair": {"price_usd": 1.0, "liq_usd": 50000}}))
    assert db.open_positions()[0]["status"] == "pending" and db.open_positions()[0]["qty"] == 0
    t[0] += 86400
    asyncio.run(ex.expire_pending())
    assert db.open_positions()[0]["status"] == "pending"     # 0 = no confirm window, never auto-expires


def test_confirm_min_still_expires_when_positive(db):
    db.set("desk.mode", "LIVE")
    db.x("INSERT OR REPLACE INTO settings VALUES('desk.live_stack','1240',0)")
    db.set("executor.confirm_min", 5)
    n = Notifier(db, Bus(db))
    t = [1000.0]
    ex = Executor(db, Bus(db), FakeSrc(price=1.0, liq=50000), n, clock=lambda: t[0], sleep=no_sleep)
    db.insert("coins", {"mint": "L5", "symbol": "L5"})
    asyncio.run(ex.process({"mint": "L5", "pair": {"price_usd": 1.0, "liq_usd": 50000}}))
    t[0] += 5 * 60 - 1
    asyncio.run(ex.expire_pending())
    assert db.open_positions()[0]["status"] == "pending"
    t[0] += 2
    asyncio.run(ex.expire_pending())
    assert db.one("SELECT status FROM positions")["status"] == "expired"


def test_max_open_five_on_190_cash(db):
    """8% of ~$190 is ~$15; five tickets fit, there is no hidden cap of 2, a sixth is refused."""
    db.set("executor.max_open", 5)
    db.move_cash(190 - db.cash(), "set cash 190", "PAPER")
    assert abs(db.cash() - 190) < 1e-9
    n = Notifier(db, Bus(db))
    ex = Executor(db, Bus(db), FakeSrc(price=1.0, liq=50000), n, sleep=no_sleep)
    for i in range(6):
        db.insert("coins", {"mint": f"M{i}", "symbol": f"T{i}"})
        asyncio.run(ex.process({"mint": f"M{i}", "pair": {"price_usd": 1.0, "liq_usd": 50000}}))
    opens = db.open_positions()
    assert len(opens) == 5
    assert all(p["status"] == "open" and p["size_usd"] >= 10 for p in opens)
    assert db.cash() > 100                                          # leftover cash, not a false refusal
    assert db.one("SELECT status_reason FROM coins WHERE mint='M5'")["status_reason"] == "5 positions already open"
    assert sum(p["size_usd"] for p in opens) == round(190 - db.cash(), 2)


class _BoomSrc:
    def __init__(self):
        self.calls = 0

    async def dex_pairs(self, mints):
        self.calls += 1
        raise FetchError("HTTP 429", 429)


def test_dex_429_backoff(db):
    """Scanner, Risk and Executor all sleep risk.backoff_429_s on a DexScreener 429 (needed at poll 5s)."""
    slept = []

    async def rec(s, *_a, **_k):
        slept.append(s)

    db.set("risk.backoff_429_s", 30)
    src, n = _BoomSrc(), Notifier(db, Bus(db))
    db.insert("coins", {"mint": "R4", "symbol": "R4"})
    db.insert("positions", {"mint": "R4", "mode": "PAPER", "opened_at": 0, "entry_price": 1.0, "size_usd": 16,
                            "qty": 16, "status": "open", "stop_pct": 15, "stop_price": 0.85, "peak_price": 1.0})
    r = Risk(db, Bus(db), src, n, sleep=rec)
    assert asyncio.run(r.tick()) == []
    assert slept == [30] and src.calls == 1

    slept.clear()
    db.insert("coins", {"mint": "S4", "status": "waiting", "migrated_at": 1})
    asyncio.run(Scanner(db, Bus(db), src, n, sleep=rec).tick())
    assert slept == [30]

    slept.clear()
    db.insert("coins", {"mint": "E4", "symbol": "E4"})
    assert asyncio.run(Executor(db, Bus(db), src, n, sleep=rec).process(
        {"mint": "E4", "pair": {"price_usd": 1.0, "liq_usd": 50000}})) is None
    assert slept == [30]
    assert db.one("SELECT status_reason FROM coins WHERE mint='E4'")["status_reason"] == "recheck: no fresh price"
