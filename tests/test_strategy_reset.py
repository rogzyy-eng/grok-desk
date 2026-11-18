"""2026-11-18 strategy fix from the PAPER data + PAPER stack reset:
entry premium guard (zincir above Dex), DexScreener price normalised to the zincir fill, breakeven lock,
and `reset 100` (PAPER only, archives instead of deleting, report/account count from the epoch)."""
import asyncio
import time

import commands
import config
import report
from agents.risk import Risk, dex_norm, breakeven_lock
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier
from tests.test_entry_price import ChainSrc, MINT, mk_ex, ref


def test_new_settings_seeded_with_ranges(db):
    assert db.get("executor.entry_max_premium_pct") == 3 and config.RANGES["executor.entry_max_premium_pct"] == (0, 100, 0.5)
    assert db.get("risk.breakeven_at_pct") == 10 and config.RANGES["risk.breakeven_at_pct"] == (0, 200, 1)
    assert "desk.paper_epoch" in commands.PROTECTED and db.get("desk.paper_epoch") is None


def test_premium_entry_skipped(db):
    ex = mk_ex(db, ChainSrc(quote=1.10))                                  # zincir 10% ABOVE Dex
    assert asyncio.run(ex.process({"mint": MINT, "pair": ref()})) is None
    assert not db.open_positions()
    why = db.one("SELECT status_reason FROM coins WHERE mint=?", (MINT,))["status_reason"]
    assert why == "pahalı giriş: zincir Dex'ten %10.0 yüksek (> %3) → girmedim"


def test_dex_norm_scales_only_zincir_fills():
    p = {"entry_native": 1.1e-6, "market_entry_price": 1.10, "dex_entry_price": 1.0}
    assert abs(dex_norm(p, 1.0) - 1.10) < 1e-12
    assert dex_norm({"entry_native": None, "market_entry_price": 1.1, "dex_entry_price": 1.0}, 1.0) == 1.0
    assert dex_norm({"entry_native": 1e-6, "market_entry_price": None, "dex_entry_price": 1.0}, 1.0) == 1.0
    assert dex_norm(p, None) is None


def risk(db, price, t=1000.0):
    return Risk(db, Bus(db), FakeSrc(price=price, liq=10**9), Notifier(db, Bus(db)), clock=lambda: t, sleep=no_sleep)


def zpos(db, entry=1.101, market=1.10, dex=1.0, native=1.1e-6):
    db.x("INSERT OR IGNORE INTO coins(mint, symbol, pool) VALUES('Z','Z','POOL')")
    return db.insert("positions", {"mint": "Z", "mode": "PAPER", "opened_at": 990.0, "entry_price": entry, "size_usd": 10,
                                   "qty": 9.82 / entry, "entry_fee": 0.18, "fees_usd": 0.18, "peak_price": entry, "stop_pct": 6,
                                   "stop_price": entry * 0.94, "trail_pct": 20, "trail_start_pct": 30, "timeout_h": 3,
                                   "runner_trail_pct": 30, "tp_pct": 100, "tp_sell_frac": 0.5, "status": "open",
                                   "entry_liq_usd": 10**9, "market_entry_price": market, "dex_entry_price": dex,
                                   "entry_native": native, "entry_quote_usd": 1e6})


def test_dex_poll_on_zincir_fill_is_not_an_instant_stop(db):
    """FYAT-style: filled at zincir 10% over the Dex quote; the first Dex poll (unchanged Dex) used to book -9% 'stop'."""
    zpos(db)
    assert asyncio.run(risk(db, 1.0).tick()) == []
    p = db.one("SELECT * FROM positions")
    assert p["status"] == "open" and abs(p["last_price"] - 1.10) < 1e-9
    out = asyncio.run(risk(db, 0.93).tick())                            # Dex really -7% from its entry quote -> stop
    assert out and out[0]["reason"] == "stop"


def test_breakeven_lock_after_peak(db):
    db.set("risk.breakeven_at_pct", 10)
    zpos(db, entry=1.0, market=None, dex=None, native=None)
    r = risk(db, 1.0)
    assert asyncio.run(r.tick({"Z": 1.12})) == []                       # +12%: stop moves up to breakeven
    p = db.one("SELECT * FROM positions")
    assert 1.03 < p["stop_price"] < 1.05
    assert "stop başabaşa" in db.one("SELECT reason FROM decisions WHERE decision='breakeven'")["reason"]
    line = p["stop_price"]
    out = asyncio.run(r.tick({"Z": line * 0.999}))                      # first tick under the raised line
    assert out[0]["reason"] == "stop"
    q = db.one("SELECT * FROM positions")
    assert q["status"] == "closed" and q["pnl_usd"] > -0.05             # ~breakeven instead of a -6% loss


def test_breakeven_lock_off_and_not_below_peak(db):
    p = {"entry_price": 1.0, "peak_price": 1.08, "qty": 9.82, "size_usd": 10, "stop_price": 0.94, "mode": "PAPER"}
    assert breakeven_lock(p, 1.07, 10**9, {"breakeven_at_pct": 10}, 0.1) is None     # peak under +10%
    assert breakeven_lock({**p, "peak_price": 1.2}, 1.1, 10**9, {"breakeven_at_pct": 0}, 0.1) is None  # off
    assert breakeven_lock({**p, "peak_price": 1.2, "runner": 1}, 1.1, 10**9, {"breakeven_at_pct": 10}, 0.1) is None
    assert breakeven_lock({**p, "peak_price": 1.2}, 1.01, 10**9, {"breakeven_at_pct": 10}, 0.1) is None  # line over price


class D:
    def __init__(self, db):
        self.db = db


def test_paper_reset_archives_and_starts_epoch(db):
    db.move_cash(-191.13, "losses", "PAPER")                             # 200 seed -> 8.87
    old = db.insert("positions", {"mint": "OLD", "mode": "PAPER", "opened_at": time.time() - 100, "closed_at": time.time() - 50,
                                  "status": "closed", "pnl_usd": -2.0, "fees_usd": 0.36, "size_usd": 10, "entry_price": 1})
    n_ledger = db.one("SELECT count(*) n FROM ledger")["n"]
    r = asyncio.run(commands.handle(D(db), "reset 100"))
    assert r["ok"] and "8.87" in r["text"] and "100.00" in r["text"]
    assert db.cash() == 100 and db.equity() == 100
    assert db.one("SELECT count(*) n FROM ledger")["n"] == n_ledger + 1  # nothing deleted
    assert db.one("SELECT id FROM positions WHERE id=?", (old,))          # old trade kept (archive)
    assert db.get("desk.paper_start_stack") == 100 and db.get("desk.paper_epoch_n") == 1
    assert db.get("desk.paper_epoch") > 0
    rep = report.build(db, 0, time.time() + 1)
    assert rep["trades"] == [] and rep["balance"] == 100                  # report counts from the reset
    with __import__("pytest").raises(commands.GateError):
        commands.set_setting(db, "desk.paper_epoch", "0")


def test_paper_reset_refused_live_or_open(db):
    db.insert("positions", {"mint": "O", "mode": "PAPER", "opened_at": 1, "status": "open", "qty": 1, "entry_price": 1, "size_usd": 10})
    r = asyncio.run(commands.handle(D(db), "paper reset 100"))
    assert not r["ok"] and "open positions" in r["text"]
    db.x("DELETE FROM positions")
    db.x("UPDATE settings SET value='\"LIVE\"' WHERE key='desk.mode'")
    r = asyncio.run(commands.handle(D(db), "reset 100"))
    assert not r["ok"] and "PAPER only" in r["text"]
    assert db.cash() == 200
    db.x("UPDATE settings SET value='\"PAPER\"' WHERE key='desk.mode'")
    assert not asyncio.run(commands.handle(D(db), "reset 5"))["ok"]       # bounds


def test_api_paper_reset_and_account_epoch(db, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    monkeypatch.setattr(config, "APP_TOKEN", "t" * 40)
    desk = D(db)
    desk.risk_lines = lambda p: (0, None, 0, False)
    c = TestClient(api.make_app(desk))
    h = {"Authorization": "Bearer " + "t" * 40}
    db.insert("positions", {"mint": "OLD", "mode": "PAPER", "opened_at": time.time() - 100, "closed_at": time.time() - 50,
                            "status": "closed", "pnl_usd": -2.0, "fees_usd": 0.36, "size_usd": 10, "entry_price": 1})
    assert c.post("/api/paper/reset", json={"stack": 100}).status_code == 401
    r = c.post("/api/paper/reset", json={"stack": 100}, headers=h)
    assert r.status_code == 200 and r.json()["new"] == 100
    a = c.get("/api/account", headers=h).json()
    assert a["closed"] == [] and a["balance"] == 100
    assert len(c.get("/api/account?all=1", headers=h).json()["closed"]) == 1
