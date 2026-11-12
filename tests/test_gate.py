"""2 test_gate — alive 0.69 / organic 0.64 / chase 0.31 / score 69 each reject; all at threshold pass.
Same in rules mode with fake DexScreener numbers; rows read "Analyst (rules)". Plus API auth + LIVE gate."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import commands
import config
import report
from agents.analyst import Analyst, gate
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier

C = config.DEFAULTS["analyst"]


def test_gate_thresholds():
    base = {"alive_6h": 0.70, "organic": 0.65, "flush": 1.0, "chase": 0.30}
    s, f = gate(base, C)
    assert not f and s == round(100 * (0.70 + 0.65 + 1.0 + 0.70) / 4)
    for k, v in (("alive_6h", 0.69), ("organic", 0.64), ("chase", 0.31)):
        s, f = gate({**base, k: v}, C)
        assert f and f[0].startswith(k), (k, f)
    # score 69 with every individual rule at threshold or better
    a = {"alive_6h": 0.70, "organic": 0.65, "flush": 0.71, "chase": 0.30}
    s, f = gate(a, C)
    assert s == 69 and f == ["score 69"]
    a["flush"] = 0.75
    s, f = gate(a, C)
    assert s == 70 and not f


def test_rules_mode_fake_dexscreener(db):
    bus = Bus(db)
    an = Analyst(db, bus, FakeSrc(), Notifier(db, bus), sleep=no_sleep)
    # alive = vol_h1/25000, organic = buys_h1/150, flush = m5 <= -5% with buys > sells, chase = h1/80
    at = {"vol_h1": 17500, "buys_h1": 97.5, "chg_m5": -6, "buys_m5": 30, "sells_m5": 10, "chg_h1": 24}
    out = asyncio.run(an.judge("M1", {**at}, {}))
    assert out["answers"] == {"alive_6h": 0.7, "organic": 0.65, "flush": 1.0, "chase": 0.3} and out["passed"]
    assert out["label"] == "Analyst (rules)"
    for k, v in (("vol_h1", 17250), ("buys_h1", 96), ("chg_h1", 24.8)):
        assert not asyncio.run(an.judge("M1", {**at, k: v}, {}))["passed"], k
    db.insert("coins", {"mint": "M2", "symbol": "RJ"})
    an.src = FakeSrc(vol_h1=100)
    asyncio.run(an.process({"mint": "M2", "scanner": {}, "auditor": {}}))
    row = db.one("SELECT * FROM activity WHERE mint='M2'")
    assert row["title"] == "Rejected RJ" and row["subtitle"].startswith("Analyst (rules) · alive_6h") and row["value"] == "Skipped"


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(config, "APP_TOKEN", "T" * 48)
    from main import Desk
    from api import make_app
    desk = Desk(db=db, src=FakeSrc())
    return TestClient(make_app(desk)), desk


H = {"Authorization": "Bearer " + "T" * 48}


def test_401_without_token(client):
    c, _ = client
    for p in ["/api/state", "/api/activity", "/api/agents", "/api/agents/risk/decisions", "/api/account", "/api/settings",
              "/api/report", "/api/health", "/api/push", "/api/chart?range=1D", "/api/coin/x", "/api/audit/x", "/api/stream"]:
        assert c.get(p).status_code == 401, p
        assert c.get(p + ("&" if "?" in p else "?") + "t=" + "T" * 48).status_code == 401, p
    for p in ["/api/pause", "/api/resume", "/api/mode", "/api/command", "/api/settings", "/api/push", "/api/push/test"]:
        assert c.post(p, json={}).status_code == 401, p
    assert c.get("/api/state", headers=H).status_code == 200


def test_live_needs_paper_night_and_typed_live(client):
    c, desk = client
    db = desk.db
    assert c.post("/api/mode", json={"mode": "LIVE", "confirm": "LIVE"}, headers=H).status_code == 403
    db.x("UPDATE settings SET value='{\"passed\": true}' WHERE key='desk.layer8_proof'")
    start, end = report.window(db)
    db.x("UPDATE settings SET value=? WHERE key='desk.paper_since'", (str(start - 3600),))
    assert report.close_night(db, now=end + 60)["full_paper_night"] and db.get("desk.paper_nights") == 1
    assert c.post("/api/mode", json={"mode": "LIVE", "confirm": "live"}, headers=H).status_code == 403
    assert c.post("/api/mode", json={"mode": "LIVE", "confirm": "LIVE"}, headers=H).status_code == 200


def test_commands(client):
    c, desk = client
    r = c.post("/api/command", json={"text": "set stop to -8%"}, headers=H).json()
    assert r["text"] == "stop -6% -> -8%" and desk.db.get("risk.stop_pct") == 8
    assert c.post("/api/command", json={"text": "invite"}, headers=H).json()["text"] == "https://fomo.family/r/savipww"
    desk.db.insert("positions", {"mint": "P", "status": "open", "mode": "PAPER", "entry_price": 1, "size_usd": 47.5, "qty": 1,
                                 "opened_at": 0, "stop_pct": 6})
    assert not c.post("/api/command", json={"text": "pause risk"}, headers=H).json()["ok"]
    assert c.post("/api/command", json={"text": "pause scanner"}, headers=H).json()["ok"]
    assert "scanner" in desk.db.get("desk.paused_agents")
    assert "commands:" in c.post("/api/command", json={"text": "hello"}, headers=H).json()["text"]
    assert c.post("/api/settings", json={"key": "desk.mode", "value": "LIVE"}, headers=H).status_code == 400
    assert c.post("/api/settings", json={"key": "executor.min_ticket_usd", "value": "4"}, headers=H).status_code == 400
    assert c.post("/api/settings", json={"key": "auditor.enabled", "value": "off"}, headers=H).status_code == 400
    assert c.post("/api/settings", json={"key": "auditor.enabled", "value": "off", "confirm_twice": True}, headers=H).status_code == 200
