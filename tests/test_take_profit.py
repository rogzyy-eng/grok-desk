"""Take-profit partial sale: risk.tp_pct / tp_sell_frac, runner breakeven stop and trail,
no double half-sale, and the HAPPY immediate-exit regression."""
import asyncio

import config
import fees
from agents.risk import Risk, breakeven_stop_price
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier
from tests.test_loss_fixes import DISC, buy, desk
from tests.test_onchain import mk as onchain_mk, open_pos as onchain_open, state as onchain_state


LIQ = 10**9


def test_settings_defaults_and_ranges(db):
    s = db.section("risk")
    assert s["tp_pct"] == 54 and s["tp_sell_frac"] == 0.5
    assert config.RANGES["risk.tp_pct"] == (0, 1000, 1)
    assert config.RANGES["risk.tp_sell_frac"] == (0.1, 1, 0.05)
    assert config.DEFAULTS["risk"]["tp_pct"] == 54
    # INSERT OR IGNORE: a second open keeps the existing value
    db.set("risk.tp_pct", 40)
    from db import DB
    DB(db.path)
    assert db.get("risk.tp_pct") == 40


def test_tp_fires_once(db):
    t = [100.0]
    ex, r, n = desk(db, t, src=FakeSrc(price=1.0, liq=LIQ))
    p = buy(ex, db, "TP", liq=LIQ)
    assert (p["tp_pct"], p["tp_sell_frac"]) == (54, 0.5)
    e, qty = p["entry_price"], p["qty"]
    db.set("risk.tp_pct", 80)                                  # later live change must not move the snapshot
    assert asyncio.run(r.tick({"TP": e * 1.50})) == []          # just under +54%
    cash0 = db.cash()
    out = asyncio.run(r.tick({"TP": e * 1.54}))
    assert out[0]["reason"] == "tp" and out[0]["status"] == "partial"
    q = db.one("SELECT * FROM positions WHERE mint='TP'")
    assert q["status"] == "open" and q["runner"] == 1 and q["trail_active"] == 1
    assert abs(q["qty"] - qty / 2) < 1e-12
    assert q["peak_price"] == e * 1.54 and q["tp_pct"] == 54    # snapshot held; peak = fill
    g1, _ = fees.sell_impact(qty / 2, e * 1.54, LIQ)
    f1 = fees.side_fee(max(g1, fees.MIN_TRADE), DISC)
    assert abs(q["partial_usd"] - (g1 - f1)) < 1e-6
    assert abs(db.cash() - (cash0 + g1 - f1)) < 1e-6
    title = db.one("SELECT title FROM activity WHERE mint='TP' AND kind='sold' ORDER BY id DESC")["title"]
    assert title.startswith("kâr al: +%54 · yarısı satıldı · +$")
    assert any("kâr al: +%54 · yarısı satıldı" in (s.get("body") or "") for s in n.sent)
    # same price again, and a higher one: TP must not fire a second time
    assert asyncio.run(r.tick({"TP": e * 1.54})) == []
    assert asyncio.run(r.tick({"TP": e * 2.0})) == []
    z = db.one("SELECT * FROM positions WHERE mint='TP'")
    assert z["status"] == "open" and z["runner"] == 1 and abs(z["qty"] - qty / 2) < 1e-12
    assert abs(z["partial_usd"] - (g1 - f1)) < 1e-6
    assert db.one("SELECT count(*) AS n FROM decisions WHERE mint='TP' AND decision='tp'")["n"] == 1


def test_runner_breakeven_stop(db):
    t = [100.0]
    ex, r, _ = desk(db, t, src=FakeSrc(price=1.0, liq=LIQ))
    db.set("risk.runner_trail_pct", 80)                         # trail well below entry, so the BE stop binds
    p = buy(ex, db, "BE", liq=LIQ)
    e = p["entry_price"]
    assert p["runner_trail_pct"] == 80
    out = asyncio.run(r.tick({"BE": e * 1.54}))
    assert out[0]["reason"] == "tp"
    q = db.one("SELECT * FROM positions WHERE mint='BE'")
    remain, sold = q["qty"], q["partial_qty"]
    be = breakeven_stop_price({**p, "mode": "PAPER"}, remain, sold, LIQ, DISC)
    assert q["stop_price"] >= be - 1e-9
    assert q["stop_price"] > e                                 # above entry: fees on the remaining half
    assert q["stop_price"] > p["stop_price"]                   # raised, never loosened
    assert asyncio.run(r.tick({"BE": q["stop_price"] * 1.01})) == []
    out = asyncio.run(r.tick({"BE": q["stop_price"] * 0.999}))
    assert out[0]["reason"] == "stop" and out[0]["runner"]
    z = db.one("SELECT * FROM positions WHERE mint='BE'")
    assert z["status"] == "closed"
    # runner stopped at breakeven on its share: the locked TP profit is kept
    assert z["pnl_usd"] > 0


def test_runner_trail_exit(db):
    t = [100.0]
    ex, r, n = desk(db, t, src=FakeSrc(price=1.0, liq=LIQ))
    p = buy(ex, db, "RT", liq=LIQ)
    e, qty, efee = p["entry_price"], p["qty"], p["entry_fee"]
    out = asyncio.run(r.tick({"RT": e * 1.54}))
    assert out[0]["reason"] == "tp"
    assert asyncio.run(r.tick({"RT": e * 2.0})) == []          # runner rides; new peak
    assert asyncio.run(r.tick({"RT": e * 2.0 * 0.80})) == []   # 20% off: default 12% trail would sell; runner 30% holds
    out = asyncio.run(r.tick({"RT": e * 2.0 * 0.69}))          # 31% off the post-TP high
    assert out[0]["reason"] == "trail" and out[0]["runner"]
    assert any("runner trail: kalan satıldı" in (s.get("body") or "") for s in n.sent)
    title = db.one("SELECT title FROM activity WHERE mint='RT' AND kind='sold' ORDER BY id DESC")["title"]
    assert title == "RT: runner trail: kalan satıldı"
    z = db.one("SELECT * FROM positions WHERE mint='RT'")
    g1, _ = fees.sell_impact(qty / 2, e * 1.54, LIQ)
    f1 = fees.side_fee(max(g1, fees.MIN_TRADE), DISC)
    g2, _ = fees.sell_impact(qty / 2, e * 2.0 * 0.69, LIQ)
    f2 = fees.side_fee(max(g2, fees.MIN_TRADE), DISC)
    assert z["status"] == "closed" and z["exit_reason"] == "trail"
    assert abs(z["pnl_usd"] - round((g1 - f1) + (g2 - f2) - 16, 4)) < 1e-6
    assert abs(z["fees_usd"] - round(efee + f1 + f2, 4)) < 1e-9


def test_no_double_half_sale(db):
    t = [100.0]
    ex, r, _ = desk(db, t, src=FakeSrc(price=1.0, liq=LIQ))
    p = buy(ex, db, "ND", liq=LIQ)
    e, qty = p["entry_price"], p["qty"]
    out = asyncio.run(r.tick({"ND": e * 1.54}))
    assert out[0]["reason"] == "tp"
    # a drop that would be a 'first trail hit' on a full position must sell the REST, not half again
    out = asyncio.run(r.tick({"ND": e * 1.54 * 0.69}))
    assert len(out) == 1 and out[0]["reason"] == "trail" and out[0]["runner"]
    z = db.one("SELECT * FROM positions WHERE mint='ND'")
    assert z["status"] == "closed" and z["exit_reason"] == "trail"
    assert abs(z["qty"] - qty / 2) < 1e-12                     # qty frozen at the remainder; not halved again
    assert db.one("SELECT count(*) AS n FROM decisions WHERE mint='ND' AND decision IN ('tp','partial')")["n"] == 1
    assert db.one("SELECT count(*) AS n FROM decisions WHERE mint='ND' AND decision='trail'")["n"] == 1

    # the other way: first-trail half-sale, then a +54% pump must not also fire TP
    t[0] = 200.0
    q = buy(ex, db, "ND2", liq=LIQ)
    e2 = q["entry_price"]
    asyncio.run(r.tick({"ND2": e2 * 1.25}))
    out = asyncio.run(r.tick({"ND2": e2 * 1.25 * 0.87}))
    assert out[0]["reason"] == "partial"
    assert asyncio.run(r.tick({"ND2": e2 * 2.0})) == []        # would be TP if the runner weren't already one
    z2 = db.one("SELECT * FROM positions WHERE mint='ND2'")
    assert z2["status"] == "open" and z2["runner"] == 1
    assert db.one("SELECT count(*) AS n FROM decisions WHERE mint='ND2' AND decision='tp'")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM decisions WHERE mint='ND2' AND decision='partial'")["n"] == 1


def test_happy_immediate_exit_regression(db):
    """HAPPY (2026-11-12): peaked +61%, gapped through the 20% trail to ~+4%. The half-sale and
    the runner sale both landed in the same minute because the runner still trailed the stale
    peak (1.61 × 0.70 = 1.127 > 1.04). Peak now restarts at the fill, so the same price holds.
    TP is off so this isolates the gap-through-trail path (HAPPY predated take-profit)."""
    t = [100.0]
    db.set("risk.tp_pct", 0)
    db.set("risk.trail_start_pct", 30)
    db.set("risk.trail_pct", 20)
    db.set("risk.runner_trail_pct", 30)
    db.set("risk.stop_pct", 15)
    ex, r, _ = desk(db, t, src=FakeSrc(price=1.0, liq=LIQ))
    p = buy(ex, db, "HAPPY", liq=LIQ)
    e, qty = p["entry_price"], p["qty"]
    assert (p["tp_pct"], p["trail_start_pct"], p["trail_pct"], p["runner_trail_pct"], p["stop_pct"]) == (0, 30, 20, 30, 15)
    assert asyncio.run(r.tick({"HAPPY": e * 1.61})) == []       # peak +61%, trail armed
    # gap to +4%: 20% trail from 1.61 fires (line 1.288). Stale runner line would be 1.127 — also below 1.04.
    out = asyncio.run(r.tick({"HAPPY": e * 1.04}))
    assert out[0]["reason"] == "partial"
    q = db.one("SELECT * FROM positions WHERE mint='HAPPY'")
    assert q["status"] == "open" and q["runner"] == 1
    assert abs(q["peak_price"] - e * 1.04) < 1e-12             # peak reset to the fill, not 1.61
    assert abs(q["qty"] - qty / 2) < 1e-12
    # same price again (next poll / same minute): must NOT dump the runner
    assert asyncio.run(r.tick({"HAPPY": e * 1.04})) == []
    # 10% off the fill, still above the -15% stop (0.85): must hold. (1.04*0.80 would be under the stop.)
    assert asyncio.run(r.tick({"HAPPY": e * 1.04 * 0.90})) == []
    out = asyncio.run(r.tick({"HAPPY": e * 1.04 * 0.69}))         # 31% off the fill (also under the 15% stop)
    assert out[0]["reason"] in ("trail", "stop") and out[0]["runner"]
    assert db.one("SELECT status FROM positions WHERE mint='HAPPY'")["status"] == "closed"


def test_tp_and_happy_gap_on_chain_and_poll(db):
    """Both price sources share assess(); TP on a websocket tick, and a HAPPY-style gap on a poll,
    must behave the same."""
    # --- on-chain TP ---
    r, n = onchain_mk(db)
    onchain_open(db, "M")
    db.x("UPDATE positions SET tp_pct=54, tp_sell_frac=0.5 WHERE mint='M'")
    st = onchain_state()                                       # price $1.00, trusted
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    assert db.one("SELECT status FROM positions WHERE mint='M'")["status"] == "open"
    # raise the on-chain price clearly through +54% (38.5/25000*1000 is 1.53999… in float)
    st.quote, st.coin, st.quote_usd = 40.0, 25000.0, 1000.0    # 40/25000*1000 = 1.60
    st.record(1000.0)
    out_price = 40.0 / 25000.0 * 1000
    asyncio.run(r.on_ws("M", st, "quote"))
    q = db.one("SELECT * FROM positions WHERE mint='M'")
    assert q["runner"] == 1 and q["status"] == "open" and q["price_source"] == "zincir"
    assert abs(q["partial_price"] - out_price) < 1e-9
    assert q["peak_price"] == q["partial_price"]
    assert any("kâr al:" in (s.get("body") or "") and "fiyat: zincir" in (s.get("body") or "") for s in n.sent)
    # same on-chain price again: no second partial
    asyncio.run(r.on_ws("M", st, "quote"))
    assert db.one("SELECT count(*) AS n FROM decisions WHERE mint='M' AND decision='tp'")["n"] == 1

    # --- DexScreener poll: HAPPY-style gap after a high, with TP off on this second coin ---
    t = [2000.0]
    db.set("risk.tp_pct", 0)
    db.set("risk.trail_start_pct", 30)
    db.set("risk.trail_pct", 20)
    db.set("risk.runner_trail_pct", 30)
    bus = Bus(db)
    r2 = Risk(db, bus, None, Notifier(db, bus), clock=lambda: t[0], sleep=no_sleep)
    db.insert("coins", {"mint": "HP", "symbol": "HP"})
    db.insert("positions", {"mint": "HP", "mode": "PAPER", "opened_at": 2000.0, "entry_price": 1.0,
                            "size_usd": 16, "qty": 15.712, "entry_fee": 0.288, "fees_usd": 0.288,
                            "peak_price": 1.0, "stop_pct": 15, "stop_price": 0.85, "trail_pct": 20,
                            "trail_start_pct": 30, "timeout_h": 3, "runner_trail_pct": 30,
                            "tp_pct": 0, "tp_sell_frac": 0.5, "status": "open", "entry_liq_usd": LIQ})
    assert asyncio.run(r2.tick({"HP": 1.61})) == []
    out = asyncio.run(r2.tick({"HP": 1.04}))
    assert out[0]["reason"] == "partial"
    assert asyncio.run(r2.tick({"HP": 1.04})) == []
    z = db.one("SELECT * FROM positions WHERE mint='HP'")
    assert z["status"] == "open" and z["runner"] == 1 and abs(z["peak_price"] - 1.04) < 1e-12


def test_pulled_sells_remainder_after_tp(db):
    t = [100.0]
    ex, r, _ = desk(db, t, src=FakeSrc(price=1.0, liq=LIQ))
    p = buy(ex, db, "RG", liq=LIQ)
    e = p["entry_price"]
    assert asyncio.run(r.tick({"RG": {"price": e * 1.54, "liq": LIQ}}))[0]["reason"] == "tp"
    out = asyncio.run(r.tick({"RG": {"price": e * 1.54, "liq": 0}}))
    assert out[0]["reason"] == "pulled" and out[0]["runner"]
    assert db.one("SELECT status FROM positions WHERE mint='RG'")["status"] == "closed"


def test_tp_off(db):
    t = [100.0]
    db.set("risk.tp_pct", 0)
    ex, r, _ = desk(db, t, src=FakeSrc(price=1.0, liq=LIQ))
    p = buy(ex, db, "OFF", liq=LIQ)
    assert p["tp_pct"] == 0
    e = p["entry_price"]
    assert asyncio.run(r.tick({"OFF": e * 1.80})) == []
    z = db.one("SELECT * FROM positions WHERE mint='OFF'")
    assert z["status"] == "open" and not z["runner"]
