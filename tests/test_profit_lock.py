"""2026-11-19 loss review (reset epoch 1 + 133 earlier zincir fills): profit-lock ladder, falling-knife entry
skip, fill-drop exit on a distrusted first on-chain price, report window when read a little before report_time."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import commands
import config
import report
from agents.executor import falling_knife_reason
from agents.risk import profit_lock
from tests.test_entry_price import ChainSrc, MINT, mk_ex, mk_risk, pos, ref, st_
from tests.test_strategy_reset import risk, zpos

C = {"lock1_at_pct": 15, "lock1_pct": 6, "lock2_at_pct": 25, "lock2_pct": 12}


def test_new_settings_seeded_with_ranges(db):
    for k, v in {"risk.lock1_at_pct": 15, "risk.lock1_pct": 6, "risk.lock2_at_pct": 25, "risk.lock2_pct": 12,
                 "risk.fill_drop_exit_pct": 15, "executor.entry_min_m5_pct": -15}.items():
        assert db.get(k) == v and k in config.RANGES
    commands.set_setting(db, "risk.lock2_pct", 10, "test")
    assert db.get("risk.lock2_pct") == 10


def test_profit_lock_steps():
    p = {"entry_price": 1.0, "peak_price": 1.14, "stop_price": 0.94}
    assert profit_lock(p, 1.10, C) is None                                       # peak under +15%
    assert abs(profit_lock({**p, "peak_price": 1.16}, 1.10, C) - 1.06) < 1e-12    # +15% -> stop +6%
    assert abs(profit_lock({**p, "peak_price": 1.30}, 1.20, C) - 1.12) < 1e-12    # +25% -> stop +12%
    assert profit_lock({**p, "peak_price": 1.30, "runner": 1}, 1.20, C) is None   # runner keeps its trail
    assert profit_lock({**p, "peak_price": 1.30, "stop_price": 1.13}, 1.20, C) is None   # never down
    assert profit_lock({**p, "peak_price": 1.30}, 1.11, C) is None               # line over the price
    assert profit_lock({**p, "peak_price": 1.30}, 1.20, {**C, "lock1_pct": 0, "lock2_pct": 0}) is None


def test_lock_turns_a_giveback_into_a_win(db):
    """SHUSHED-style: +24% peak, then back to entry. Before: breakeven stop ~ -$0.35. Now the +6% lock sells."""
    zpos(db)
    e = 1.101
    assert asyncio.run(risk(db, 1.0).tick({"Z": e * 1.24})) == []
    p = db.one("SELECT * FROM positions")
    assert abs(p["stop_price"] - e * 1.06) < 1e-9
    assert "kâr kilidi: stop +%6" in db.one("SELECT reason FROM decisions WHERE decision='lock'")["reason"]
    out = asyncio.run(risk(db, 1.0).tick({"Z": e * 1.055}))
    assert out and out[0]["reason"] == "stop"
    q = db.one("SELECT * FROM positions")
    assert q["status"] == "closed" and q["pnl_usd"] > 0.1
    t = db.one("SELECT reason FROM decisions WHERE decision='stop'")["reason"]
    assert "kâr kilidi/başabaş stop (+%6)" in t and "-6% stop" not in t


def test_falling_knife_reason():
    assert falling_knife_reason(-18.5, -15) == "düşen bıçak: 5dk %-18 < %-15 → girmedim"
    assert falling_knife_reason(-14.9, -15) is None
    assert falling_knife_reason(None, -15) is None
    assert falling_knife_reason(-40, 0) is None                                    # 0 = off


def test_falling_knife_entry_skipped(db):
    src = ChainSrc()
    src.kw["chg_m5"] = -20
    ex = mk_ex(db, src)
    assert asyncio.run(ex.process({"mint": MINT, "pair": ref()})) is None
    assert not db.open_positions()
    assert db.one("SELECT status_reason FROM coins WHERE mint=?", (MINT,))["status_reason"].startswith("düşen bıçak")


def test_distrusted_drop_vs_own_fill_exits(db):
    """GRS: first zincir price -22% vs our zincir fill, no Dex native to compare -> sell at the zincir price."""
    r, n = mk_risk(db)
    pos(db, entry=0.001, entry_native=1e-6, quote_usd=1000.0)
    st = st_(1_000_000, 0.78, usd=1000.0)
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    p = db.one("SELECT * FROM positions")
    assert st.trusted is False and p["status"] == "closed" and p["exit_reason"] == "rug_early"
    assert abs(p["exit_price"] - 0.00078) < 1e-9 and p["price_source"] == "zincir"
    assert any("zincir fiyatı girişe göre %22 düşük → çıktım" in s["body"] for s in n.sent if s.get("body"))


def test_fill_drop_exit_off(db):
    db.set("risk.fill_drop_exit_pct", 0)
    r, _ = mk_risk(db)
    pos(db, entry=0.001, entry_native=1e-6, quote_usd=1000.0)
    st = st_(1_000_000, 0.78, usd=1000.0)
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    assert st.trusted is False and db.one("SELECT status FROM positions")["status"] == "open"


def test_report_read_just_before_report_time_counts_tonight(db):
    """19 Nov: routine read /api/report at ~07:59 -> window was the night ending 18 Nov 08:00, which lay before
    the reset epoch -> '0 trades' although 10 trades had closed."""
    tz = ZoneInfo("Europe/Istanbul")
    ts = lambda *a: datetime(2026, 11, *a, tzinfo=tz).timestamp()
    db.set("desk.paper_epoch", ts(18, 22, 15))
    db.insert("positions", {"mint": "R", "mode": "PAPER", "opened_at": ts(18, 22, 35), "closed_at": ts(18, 22, 36),
                            "entry_price": 1, "size_usd": 10, "status": "closed", "pnl_usd": -1.0, "fees_usd": 0.36,
                            "exit_reason": "stop"})
    s, e = report.window(db, now=ts(19, 7, 59))
    assert e == ts(19, 8, 0) and s == ts(18, 8, 0)
    assert len(report.build(db, s, e)["trades"]) == 1
    s2, e2 = report.window(db, now=ts(19, 12, 0))                                 # later in the day: same night
    assert (s2, e2) == (s, e)
    s3, e3 = report.window(db, now=ts(19, 5, 0))                                  # 3 h before: still last night
    assert e3 == ts(18, 8, 0)


def test_trail_still_first_above_the_lock(db):
    """Live trail 30/20: a +50% peak puts the trail line (+20%) over the +12% lock -> half-sale, not a full stop."""
    zpos(db)
    e = 1.101
    assert asyncio.run(risk(db, 1.0).tick({"Z": e * 1.5})) == []
    out = asyncio.run(risk(db, 1.0).tick({"Z": e * 1.19}))
    assert out and out[0]["reason"] == "partial"


def test_decisions_api_limit_and_mint(db, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    from tests.test_strategy_reset import D
    monkeypatch.setattr(config, "APP_TOKEN", "t" * 40)
    desk = D(db)
    desk.risk_lines = lambda p: (0, None, 0, False)
    c = TestClient(api.make_app(desk))
    h = {"Authorization": "Bearer " + "t" * 40}
    for i in range(30):
        db.insert("decisions", {"ts": i, "agent": "risk", "mint": "A" if i % 2 else "B", "decision": "x", "reason": str(i)})
    assert len(c.get("/api/agents/risk/decisions", headers=h).json()["items"]) == 20
    assert len(c.get("/api/agents/risk/decisions?limit=100", headers=h).json()["items"]) == 30
    only = c.get("/api/agents/risk/decisions?limit=100&mint=A", headers=h).json()["items"]
    assert len(only) == 15 and {r["mint"] for r in only} == {"A"}
