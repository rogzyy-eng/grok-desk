"""3 test_risk — +25% then 13% off the top = trail; -6.1% = stop; 3h at +4% = timeout; loosening the
stop never moves an open stop down; "set stop to -8%" → new position reads -8%, the old one -6%.
Since 2026-11-12 the first trail hit sells half and the runner trails 30%; open stops are a snapshot."""
import asyncio

import commands
from agents.executor import Executor
from agents.risk import Risk
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier


class D:
    pass


def mk(db, t):
    bus = Bus(db)
    n = Notifier(db, bus)
    return Risk(db, bus, None, n, clock=lambda: t[0], sleep=no_sleep), n


def open_pos(db, mint, stop_pct=None, t=0.0):
    sp = stop_pct if stop_pct is not None else db.get("risk.stop_pct")
    db.insert("coins", {"mint": mint, "symbol": mint})
    return db.insert("positions", {"mint": mint, "mode": "PAPER", "opened_at": t, "entry_price": 1.0, "size_usd": 47.5,
                                   "qty": 46.645, "entry_fee": 0.855, "fees_usd": 0.855, "peak_price": 1.0, "stop_pct": sp,
                                   "stop_price": 1 - sp / 100, "status": "open"})


def test_trail_stop_timeout(db):
    t = [100.0]
    r, n = mk(db, t)
    open_pos(db, "TR", t=100); open_pos(db, "ST", t=100)
    asyncio.run(r.tick({"TR": 1.25, "ST": 0.95}))
    out = asyncio.run(r.tick({"TR": 1.25 * 0.87, "ST": 0.939}))   # 13% off the top; -6.1%
    reasons = {o["mint"]: o for o in out}
    assert reasons["TR"]["reason"] == "partial" and reasons["ST"]["reason"] == "stop"   # first trail hit sells half
    assert reasons["TR"]["exitPrice"] == 1.25 * 0.87                # exit at the polled price
    assert any(s["body"].startswith("PAPER · Sold half TR · +$") for s in n.sent if s.get("body"))
    assert asyncio.run(r.tick({"TR": 2.0})) == []                  # the runner keeps riding
    assert asyncio.run(r.tick({"TR": 2.0 * 0.71})) == []           # 29% off the top still holds (30% runner trail)
    out = asyncio.run(r.tick({"TR": 2.0 * 0.69}))
    assert out[0]["reason"] == "trail" and out[0]["runner"] is True
    assert any(s["body"].startswith("PAPER · Sold TR (runner) · ") for s in n.sent if s.get("body"))
    assert any(s["body"] == f"PAPER · ST hit the -6% stop · -${abs(reasons['ST']['pnlUsd']):.2f}" for s in n.sent if s.get("body"))
    open_pos(db, "TO", t=100)
    t[0] = 100 + 3 * 3600
    out = asyncio.run(r.tick({"TO": 1.04}))
    assert out[0]["reason"] == "timeout"
    assert db.one("SELECT title, subtitle FROM activity WHERE mint='ST'") == {"title": "Stopped ST", "subtitle": "Risk · -6% stop"}


def test_stop_never_loosens_and_set_stop_8(db):
    t = [100.0]
    r, n = mk(db, t)
    old = open_pos(db, "OLD", t=100)
    commands.set_setting(db, "stop", "-8%")
    asyncio.run(r.tick({"OLD": 0.99}))
    p = db.one("SELECT * FROM positions WHERE id=?", (old,))
    assert p["stop_pct"] == 6 and abs(p["stop_price"] - 0.94) < 1e-9     # loosening never moves an open stop down
    ex = Executor(db, Bus(db), FakeSrc(price=1.0), n, sleep=no_sleep)
    db.insert("coins", {"mint": "NEW", "symbol": "NEW"})
    pid = asyncio.run(ex.process({"mint": "NEW", "pair": {"price_usd": 1.0, "liq_usd": 50000}}))
    assert db.one("SELECT stop_pct FROM positions WHERE id=?", (pid,))["stop_pct"] == 8
    asyncio.run(r.tick({"OLD": 0.99, "NEW": 0.919}))                     # -8.1%
    assert any(s["body"].startswith("PAPER · NEW hit the -8% stop") for s in n.sent if s.get("body"))
    assert db.one("SELECT subtitle FROM activity WHERE mint='NEW' AND kind='stopped'")["subtitle"].startswith("Risk · -8% stop · impact ")
    assert db.one("SELECT stop_pct FROM positions WHERE id=?", (old,))["stop_pct"] == 6
    from main import Desk
    from fastapi.testclient import TestClient
    import config
    config.APP_TOKEN = "Z" * 40
    from api import make_app
    c = TestClient(make_app(Desk(db=db, src=FakeSrc())))
    risk_card = [a for a in c.get("/api/agents", headers={"Authorization": "Bearer " + "Z" * 40}).json()["agents"] if a["name"] == "risk"][0]
    assert "stop -8%" in risk_card["pills"]
    commands.set_setting(db, "stop", "-5%")                              # snapshot: open positions keep their stop
    asyncio.run(r.tick({"OLD": 0.99}))
    assert db.one("SELECT stop_pct FROM positions WHERE id=?", (old,))["stop_pct"] == 6


def test_no_price_never_invented_and_pulled(db):
    t = [100.0]
    r, n = mk(db, t)
    open_pos(db, "NP", t=100)
    for _ in range(4):
        asyncio.run(r.tick({}))
    assert any("NP: no price for 1 min" in (s.get("body") or "") for s in n.sent)
    assert db.one("SELECT status FROM positions")["status"] == "open"
    out = asyncio.run(r.tick({"NP": {"price": 0.97, "liq": 0}}))
    assert out[0]["reason"] == "pulled"


def test_live_exit_waits_for_sold_and_repushes_once(db):
    db.set("desk.mode", "LIVE")
    t = [100.0]
    r, n = mk(db, t)
    pid = open_pos(db, "LV", t=100)
    db.x("UPDATE positions SET mode='LIVE' WHERE id=?", (pid,))
    asyncio.run(r.tick({"LV": 0.5}))
    assert db.one("SELECT status FROM positions")["status"] == "exiting"
    t[0] += 121
    asyncio.run(r.tick({})); asyncio.run(r.tick({}))
    assert sum(1 for s in n.sent if "still open" in (s.get("body") or "")) == 1
    assert not r.paused()
