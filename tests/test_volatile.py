"""Volatile coins (|5-min price change| >= risk.volatile_m5_pct at entry) get half the risk settings;
normal coins keep the plain ones. Old DBs migrate; old open positions keep working."""
import asyncio
import sqlite3

from agents.executor import Executor
from agents.risk import Risk, check_exit, lines, pos_cfg
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier


def buy(db, mint, chg_m5):
    n = Notifier(db, Bus(db))
    ex = Executor(db, Bus(db), FakeSrc(price=1.0, liq=50000, chg_m5=chg_m5), n, sleep=no_sleep)
    db.insert("coins", {"mint": mint, "symbol": mint})
    pid = ex.process({"mint": mint, "pair": {"price_usd": 1.0, "liq_usd": 50000}})
    return asyncio.run(pid), n


def test_settings_defaults(db):
    import config
    c = db.section("risk")
    assert c["volatile_m5_pct"] == 15 and c["volatile_factor"] == 0.5
    assert config.RANGES["risk.volatile_m5_pct"] == (3, 100, 1)
    assert config.RANGES["risk.volatile_factor"] == (0.1, 1, 0.05)
    assert "volatile_h1_pct" not in c


def test_normal_vs_volatile_entry(db):
    db.set("executor.max_open", 4)
    pn, n = buy(db, "NORM", 5)
    pv, n2 = buy(db, "VOL", 20)
    pd, _ = buy(db, "DUMP", -18)                      # abs(): a -18% 5-min move is volatile too
    rn, rv, rd = (db.one("SELECT * FROM positions WHERE id=?", (i,)) for i in (pn, pv, pd))
    assert rn["volatile"] == 0 and rn["stop_pct"] == 6 and abs(rn["stop_price"] - 0.94) < 1e-9
    assert rv["volatile"] == 1 and rv["stop_pct"] == 3 and abs(rv["stop_price"] - 0.97) < 1e-9 and rv["m5_change_pct"] == 20
    assert rd["volatile"] == 1
    assert "stop 6%" in n.sent[-1]["body"] and "volatil" not in n.sent[-1]["body"]
    assert "volatil" in n2.sent[-1]["body"] and "stop 3%" in n2.sent[-1]["body"]
    sub = db.one("SELECT subtitle FROM activity WHERE mint='VOL' AND kind='bought'")["subtitle"]
    assert "volatil" in sub and "stop 3%" in sub
    assert "r=savipww" in n2.sent[-1]["buttons"][0]["url"]


def test_pos_cfg_halves_only_volatile(db):
    c = db.section("risk")
    assert pos_cfg({"volatile": 0}, c) is c
    v = pos_cfg({"volatile": 1}, c)
    assert (v["stop_pct"], v["trail_start_pct"], v["trail_pct"], v["timeout_h"]) == (3, 10, 6, 1.5)
    assert pos_cfg({"volatile": 1}, v) is v                    # never scaled twice


def test_risk_exits_volatile_half(db):
    t = [100.0]
    n = Notifier(db, Bus(db))
    r = Risk(db, Bus(db), None, n, clock=lambda: t[0], sleep=no_sleep)
    base = {"mode": "PAPER", "opened_at": 100, "entry_price": 1.0, "size_usd": 16, "qty": 15.7, "entry_fee": 0.3,
            "fees_usd": 0.3, "peak_price": 1.0, "status": "open"}
    for m, vol, sp in (("N", 0, 6), ("V", 1, 3)):
        db.insert("coins", {"mint": m, "symbol": m})
        db.insert("positions", {**base, "mint": m, "volatile": vol, "stop_pct": sp, "stop_price": 1 - sp / 100})
    out = asyncio.run(r.tick({"N": 0.965, "V": 0.965}))         # -3.5%: only the volatile one stops
    assert [o["mint"] for o in out] == ["V"] and out[0]["reason"] == "stop" and out[0]["volatile"] is True
    assert db.one("SELECT subtitle FROM activity WHERE mint='V' AND kind='stopped'")["subtitle"] == "Risk · -3% stop (volatil)"
    # trail: normal arms at +20% / 12% off top; volatile at +10% / 6% off top
    db.insert("coins", {"mint": "V2", "symbol": "V2"})
    db.insert("positions", {**base, "mint": "V2", "volatile": 1, "stop_pct": 3, "stop_price": 0.97})
    asyncio.run(r.tick({"N": 1.12, "V2": 1.12}))
    out = asyncio.run(r.tick({"N": 1.12 * 0.93, "V2": 1.12 * 0.93}))
    assert [o["mint"] for o in out] == ["V2"] and out[0]["reason"] == "trail"
    # timeout: volatile 1.5h, normal 3h
    db.insert("coins", {"mint": "V3", "symbol": "V3"})
    db.insert("positions", {**base, "mint": "V3", "volatile": 1, "stop_pct": 3, "stop_price": 0.97})
    t[0] = 100 + 1.6 * 3600
    out = asyncio.run(r.tick({"N": 1.0, "V3": 1.0}))
    assert [o["mint"] for o in out] == ["V3"] and out[0]["reason"] == "timeout"


def test_old_db_migrates_and_old_positions_stay_normal(tmp_path):
    path = tmp_path / "old.db"
    cx = sqlite3.connect(path)
    cx.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY, mint TEXT, opened_at REAL, entry_price REAL, size_usd REAL, "
               "peak_price REAL, stop_price REAL, trail_active INTEGER DEFAULT 0, closed_at REAL, exit_price REAL, exit_reason TEXT, "
               "pnl_usd REAL, fees_usd REAL, mode TEXT, status TEXT, qty REAL, stop_pct REAL, entry_fee REAL, exit_line REAL, "
               "exit_requested_at REAL, repushed INTEGER DEFAULT 0, no_price_polls INTEGER DEFAULT 0, last_price REAL, signal_id INTEGER)")
    cx.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price, mode, status, qty, stop_pct) "
               "VALUES('OLD', 0, 1.0, 16, 1.0, 0.94, 'PAPER', 'open', 15, 6)")
    cx.commit(); cx.close()
    from db import DB
    db = DB(path)
    DB(path)                                                    # second start: migration is idempotent
    p = db.one("SELECT * FROM positions WHERE mint='OLD'")
    assert p["volatile"] == 0 and p["m5_change_pct"] is None
    c = db.section("risk")
    assert check_exit(p, 0.95, 100, c) is None and lines(p, c)[0] == 0.94
