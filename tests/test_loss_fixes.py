"""2026-11-12 loss analysis fixes: crashed-coin scanner kills, m5-window analyst rules, pulled-liquidity
exits, risk snapshot per position, PAPER price impact, runners (half on first trail hit)."""
import asyncio
import sqlite3

import config
import fees
from agents.analyst import rules_answers
from agents.executor import Executor
from agents.risk import Risk, pulled
from agents.scanner import Scanner
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier

DISC = 0.10


def raw_pair(mint, **kw):
    p = asyncio.run(FakeSrc(**kw).dex_pairs([mint]))[0]
    p["pairCreatedAt"] = 1_000_000   # ms; pool is old enough at t=10,000s
    return p


def scanner(db, t=10_000.0):
    bus = Bus(db)
    return Scanner(db, bus, None, Notifier(db, bus), clock=lambda: t, sleep=no_sleep)


# ---- 1. scanner kills crashed coins ----
def test_settings_and_ranges(db):
    s = db.all_settings()
    assert (s["scanner"]["max_h1_drop_pct"], s["scanner"]["max_m5_drop_pct"], s["scanner"]["max_from_high_pct"]) == (50, 40, 60)
    assert (s["analyst"]["rules_alive_volume_m5"], s["analyst"]["rules_organic_buys_m5"]) == (2000, 12)
    assert (s["analyst"]["rules_flush_max_pct"], s["analyst"]["rules_crash_h1_pct"]) == (30, 50)
    assert (s["risk"]["runner_trail_pct"], s["risk"]["pulled_liq_drop_pct"], s["risk"]["pulled_price_drop_pct"]) == (30, 50, 50)
    assert s["risk"]["volatile_factor"] == 1
    assert "rules_alive_volume_h1" not in s["analyst"] and "rules_organic_buys_h1" not in s["analyst"]
    for k in ("scanner.max_h1_drop_pct", "scanner.max_m5_drop_pct", "scanner.max_from_high_pct", "analyst.rules_alive_volume_m5",
              "analyst.rules_organic_buys_m5", "analyst.rules_flush_max_pct", "analyst.rules_crash_h1_pct",
              "risk.runner_trail_pct", "risk.pulled_liq_drop_pct", "risk.pulled_price_drop_pct"):
        assert k in config.RANGES, k


def test_scanner_kills_crashed(db):
    sc = scanner(db)
    for mint, kw, why in (("H1", {"chg_h1": -55}, "crashed -55% in 1h"), ("M5", {"chg_m5": -45}, "crashed -45% in 5 min")):
        db.insert("coins", {"mint": mint, "status": "waiting", "migrated_at": 9_000})
        assert asyncio.run(sc.evaluate(db.one("SELECT * FROM coins WHERE mint=?", (mint,)), [raw_pair(mint, **kw)])) == "kill"
        assert db.one("SELECT status_reason FROM coins WHERE mint=?", (mint,))["status_reason"] == why
    # far under its recorded high: the desk saw $1.00, now $0.35 (-65%)
    db.insert("coins", {"mint": "HI", "status": "waiting", "migrated_at": 9_000})
    db.insert("prices", {"mint": "HI", "ts": 9_500, "price": 1.0})
    assert asyncio.run(sc.evaluate(db.one("SELECT * FROM coins WHERE mint='HI'"), [raw_pair("HI", price=0.35)])) == "kill"
    assert db.one("SELECT status_reason FROM coins WHERE mint='HI'")["status_reason"] == "crashed -65% from its high"
    c = db.section("scanner")
    assert sc.crashed("HI", {"chg_h1": -49, "chg_m5": -39, "price_usd": 0.45}, c) is None   # -55% from high: allowed
    assert sc.crashed("X", {"chg_h1": -50, "chg_m5": None, "price_usd": 1}, c) == "crashed -50% in 1h"


# ---- 2. analyst rules ----
def test_rules_crash_m5_window_flush_band():
    c = config.DEFAULTS["analyst"]
    base = {"vol_m5": 2000, "buys_m5": 12, "sells_m5": 3, "chg_m5": 0, "chg_h1": 0, "vol_h1": 0, "buys_h1": 0}
    a = rules_answers(base, 0, c)
    assert a["alive_6h"] == 1.0 and a["organic"] == 1.0           # m5 window, h1 ignored
    assert rules_answers({**base, "vol_m5": 1000, "buys_m5": 6, "vol_h1": 10**6, "buys_h1": 10**4}, 0, c)["alive_6h"] == 0.5
    assert rules_answers({**base, "chg_h1": -60}, 0, c)["chase"] == 1.0     # crash penalty (was max(0, h1) = 0)
    assert rules_answers({**base, "chg_h1": -40}, 0, c)["chase"] == 0.0
    assert rules_answers({**base, "chg_h1": 40}, 0, c)["chase"] == 0.5
    for m5, f in ((-4, 0.0), (-5, 1.0), (-20, 1.0), (-30, 1.0), (-31, 0.0), (-60, 0.0)):
        assert rules_answers({**base, "chg_m5": m5}, 0, c)["flush"] == f, m5


# ---- helpers for executor / risk ----
def desk(db, t, src=None):
    bus = Bus(db)
    n = Notifier(db, bus)
    ex = Executor(db, bus, src or FakeSrc(price=1.0, liq=10000), n, clock=lambda: t[0], sleep=no_sleep)
    r = Risk(db, bus, None, n, clock=lambda: t[0], sleep=no_sleep)
    return ex, r, n


def buy(ex, db, mint, liq=10000, **kw):
    ex.src = FakeSrc(price=1.0, liq=liq, **kw)
    db.insert("coins", {"mint": mint, "symbol": mint})
    pid = asyncio.run(ex.process({"mint": mint, "pair": {"price_usd": 1.0, "liq_usd": liq}}))
    return db.one("SELECT * FROM positions WHERE id=?", (pid,))


# ---- 3. pulled liquidity ----
def test_pulled_liquidity_and_price_gap(db):
    t = [100.0]
    ex, r, _ = desk(db, t)
    db.set("executor.max_open", 4)
    p = buy(ex, db, "LQ", liq=10000)
    assert p["entry_liq_usd"] == 10000
    out = asyncio.run(r.tick({"LQ": {"price": 1.0, "liq": 5100}}))      # -49%: hold
    assert out == []
    out = asyncio.run(r.tick({"LQ": {"price": 1.0, "liq": 4900}}))      # -51% from entry: pulled
    assert out[0]["reason"] == "pulled"
    assert "liquidity -51% since entry" in db.one("SELECT subtitle FROM activity WHERE mint='LQ' AND kind='sold'")["subtitle"]
    q = buy(ex, db, "GAP", liq=10000)
    assert asyncio.run(r.tick({"GAP": 1.02})) == []
    out = asyncio.run(r.tick({"GAP": 0.50}))                            # -51% between two polls
    assert out[0]["reason"] == "pulled"
    c = db.section("risk")
    assert pulled({"entry_liq_usd": 100, "last_price": 1.0}, 0.6, 60, c) is None
    assert pulled({"entry_liq_usd": 100, "last_price": 1.0}, 0.6, 0, c) == "liquidity 0"


def test_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    cx = sqlite3.connect(path)
    cx.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY, mint TEXT, opened_at REAL, entry_price REAL, size_usd REAL, "
               "peak_price REAL, stop_price REAL, trail_active INTEGER DEFAULT 0, closed_at REAL, exit_price REAL, exit_reason TEXT, "
               "pnl_usd REAL, fees_usd REAL, mode TEXT, status TEXT, qty REAL, stop_pct REAL, entry_fee REAL, exit_line REAL, "
               "exit_requested_at REAL, repushed INTEGER DEFAULT 0, no_price_polls INTEGER DEFAULT 0, last_price REAL, signal_id INTEGER, "
               "volatile INTEGER DEFAULT 0, m5_change_pct REAL)")
    cx.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price, mode, status, qty, stop_pct, "
               "entry_fee, last_price) VALUES('OLD', 100, 1.0, 16, 1.0, 0.94, 'PAPER', 'open', 15.7, 6, 0.3, 1.0)")
    cx.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, updated_at REAL)")
    cx.execute("INSERT INTO settings VALUES('analyst.rules_alive_volume_h1', '25000', 0)")
    cx.commit(); cx.close()
    from db import DB
    db = DB(path); db = DB(path)
    p = db.one("SELECT * FROM positions WHERE mint='OLD'")
    assert p["entry_liq_usd"] is None and p["trail_pct"] is None and p["runner"] == 0
    assert db.get("analyst.rules_alive_volume_h1") is None
    t = [200.0]
    r = Risk(db, Bus(db), None, Notifier(db, Bus(db)), clock=lambda: t[0], sleep=no_sleep)
    assert asyncio.run(r.tick({"OLD": {"price": 0.97, "liq": 3000}})) == []    # no entry liq: no false 'pulled'


# ---- 4. risk snapshot ----
def test_snapshot_ignores_later_setting_changes(db):
    t = [100.0]
    ex, r, _ = desk(db, t)
    p = buy(ex, db, "SN")
    assert (p["stop_pct"], p["trail_start_pct"], p["trail_pct"], p["timeout_h"], p["runner_trail_pct"]) == (6, 20, 12, 3, 30)
    for k, v in (("risk.stop_pct", 2), ("risk.trail_start_pct", 1), ("risk.trail_pct", 1), ("risk.timeout_h", 0.5)):
        db.set(k, v)
    e = p["entry_price"]
    assert asyncio.run(r.tick({"SN": e * 0.97})) == []                  # live stop 2% would fire; snapshot 6% holds
    t[0] = 100 + 3600
    assert asyncio.run(r.tick({"SN": e * 1.01})) == []                  # live timeout 0.5h would fire; snapshot 3h holds
    q = db.one("SELECT * FROM positions WHERE mint='SN'")
    assert q["stop_pct"] == 6 and q["trail_active"] == 0                # no tighten(), live trail start 1% ignored
    import inspect
    from agents import risk as riskmod
    assert "def tighten" not in inspect.getsource(riskmod)


def test_volatile_factor_applied_at_snapshot(db):
    t = [100.0]
    ex, r, _ = desk(db, t)
    db.set("executor.max_open", 4)
    db.set("risk.volatile_factor", 0.5)
    v = buy(ex, db, "VV", chg_m5=20)
    n = buy(ex, db, "NN", chg_m5=2)
    assert (v["volatile"], v["stop_pct"], v["trail_start_pct"], v["trail_pct"], v["timeout_h"], v["runner_trail_pct"]) == (1, 3, 10, 6, 1.5, 15)
    assert (n["volatile"], n["stop_pct"], n["trail_pct"]) == (0, 6, 12)
    db.set("risk.volatile_factor", 1)                                    # later change: the open volatile one keeps 3%
    out = asyncio.run(r.tick({"VV": v["entry_price"] * 0.965, "NN": n["entry_price"] * 0.965}))
    assert [o["mint"] for o in out] == ["VV"] and out[0]["reason"] == "stop"


# ---- 5. PAPER price impact ----
def test_paper_impact_entry_and_exit(db):
    t = [100.0]
    ex, r, _ = desk(db, t)
    p = buy(ex, db, "IM", liq=8000)                                       # $16 ticket into a $4,000 quote side
    fee = fees.side_fee(16, DISC)
    f = (16 - fee) / 4000
    assert abs(p["entry_price"] - (1 + f)) < 1e-12 and p["market_entry_price"] == 1.0
    assert abs(p["entry_impact_pct"] - round(f * 100, 4)) < 1e-12
    assert abs(p["qty"] - (16 - fee) / (1 + f)) < 1e-12
    assert "impact 0.39%" in db.one("SELECT subtitle FROM activity WHERE mint='IM' AND kind='bought'")["subtitle"]
    gross, imp = fees.sell_impact(p["qty"], 0.5, 8000)
    v = p["qty"] * 0.5
    assert abs(gross - v / (1 + v / 4000)) < 1e-12 and imp > 0
    assert fees.sell_impact(10, 1.0, None) == (10.0, 0.0) and fees.sell_impact(10, 1.0, 0)[0] == 0.0
    cash0 = db.cash()
    out = asyncio.run(r.tick({"IM": {"price": 0.93, "liq": 8000}}))
    assert out[0]["reason"] == "stop"
    g, _ = fees.sell_impact(p["qty"], 0.93, 8000)
    xfee = fees.side_fee(max(g, fees.MIN_TRADE), DISC)
    assert abs(db.cash() - (cash0 + g - xfee)) < 1e-6
    row = db.one("SELECT * FROM positions WHERE mint='IM'")
    assert abs(row["pnl_usd"] - round(g - xfee - 16, 4)) < 1e-9 and abs(row["fees_usd"] - round(fee + xfee, 4)) < 1e-9
    assert "impact" in db.one("SELECT subtitle FROM activity WHERE mint='IM' AND kind='stopped'")["subtitle"]


def test_live_has_no_simulated_impact(db):
    db.set("desk.mode", "LIVE")
    db.x("INSERT OR REPLACE INTO settings VALUES('desk.live_stack','200',0)")
    t = [100.0]
    ex, _, _ = desk(db, t)
    p = buy(ex, db, "LV", liq=8000)
    assert p["entry_price"] == 1.0 and p["entry_impact_pct"] == 0.0


# ---- 6. runners ----
def test_runner_half_then_wide_trail(db):
    t = [100.0]
    ex, r, n = desk(db, t)
    p = buy(ex, db, "RN", liq=10**9)                                      # huge pool: impact ~0, exact fee maths
    e, qty, efee = p["entry_price"], p["qty"], p["entry_fee"]
    asyncio.run(r.tick({"RN": e * 1.25}))                                  # trail armed at +20%
    cash0 = db.cash()
    out = asyncio.run(r.tick({"RN": e * 1.25 * 0.87}))                     # 13% off the top: first trail hit
    assert out[0]["reason"] == "partial"
    q = db.one("SELECT * FROM positions WHERE mint='RN'")
    half_px = e * 1.25 * 0.87
    g1, _ = fees.sell_impact(qty / 2, half_px, 10**9)
    f1 = fees.side_fee(max(g1, fees.MIN_TRADE), DISC)
    assert q["status"] == "open" and q["runner"] == 1 and abs(q["qty"] - qty / 2) < 1e-12
    assert abs(q["partial_usd"] - (g1 - f1)) < 1e-6 and abs(q["partial_fee"] - f1) < 1e-9
    assert abs(db.cash() - (cash0 + g1 - f1)) < 1e-6
    assert abs(out[0]["pnlUsd"] - round(g1 - f1 - 8, 2)) < 1e-9
    assert db.one("SELECT title FROM activity WHERE mint='RN' AND kind='sold'")["title"] == "Sold half RN"
    assert asyncio.run(r.tick({"RN": e * 2.0})) == []                      # the runner rides to +100%
    assert asyncio.run(r.tick({"RN": e * 2.0 * 0.80})) == []               # 20% off: normal 12% trail would sell; runner 30% holds
    out = asyncio.run(r.tick({"RN": e * 2.0 * 0.69}))                      # 31% off the top
    assert out[0]["reason"] == "trail" and out[0]["runner"]
    z = db.one("SELECT * FROM positions WHERE mint='RN'")
    g2, _ = fees.sell_impact(qty / 2, e * 2.0 * 0.69, 10**9)
    f2 = fees.side_fee(max(g2, fees.MIN_TRADE), DISC)
    assert z["status"] == "closed"
    assert abs(z["pnl_usd"] - round((g1 - f1) + (g2 - f2) - 16, 4)) < 1e-6
    assert abs(z["fees_usd"] - round(efee + f1 + f2, 4)) < 1e-9             # fee on each side
    import report
    rep = report.build(db, 0, 10**10)
    assert len(rep["trades"]) == 1 and abs(rep["pnl_usd"] - round(z["pnl_usd"], 2)) < 1e-9


def test_half_under_min_trade_sells_all(db):
    t = [100.0]
    ex, r, _ = desk(db, t)
    db.set("executor.min_ticket_usd", 5)
    db.set("executor.stack_pct", 4)                                        # $8 ticket: a half is ~$4.6 < $5
    p = buy(ex, db, "SM", liq=10**9)
    asyncio.run(r.tick({"SM": p["entry_price"] * 1.25}))
    out = asyncio.run(r.tick({"SM": p["entry_price"] * 1.25 * 0.87}))
    assert out[0]["reason"] == "trail" and db.one("SELECT status FROM positions WHERE mint='SM'")["status"] == "closed"
