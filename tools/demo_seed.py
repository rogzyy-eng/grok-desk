"""Seed a DEMO database (for screenshots only). Uses the real CYBRON replay for the audit, plus
synthetic PAPER trades/prices clearly labelled demo. Never run against the production DB."""
import asyncio
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import DB  # noqa
from bus import Bus  # noqa


async def seed(path):
    db = DB(path)
    bus = Bus(db)
    now = time.time()
    random.seed(7)
    db.x("INSERT INTO settings(key,value,updated_at) VALUES('desk.layer8_proof',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
         (json.dumps({"passed": True, "accounts": 1182, "holders": 162}), now))
    # real replay audit for CYBRON
    from sources import ReplaySources
    from agents.auditor import Auditor
    from notify import Notifier
    mint = "A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump"
    created = 1794428021000
    db.insert("coins", {"mint": mint, "symbol": "CYBRON", "name": "CYBRON", "status": "rejected",
                        "status_reason": "flush: m5 -69.0% ≤ -5%", "migrated_at": now - 7200, "updated_at": now - 7000})
    au = Auditor(db, bus, ReplaySources(graph_mode="file"), Notifier(db, bus), clock=lambda: created / 1000 + 16 * 60)
    res = await au.audit(mint, {"pool": "DUxG3Y9BAe6kvz2kkXYtHwRpc5jDj2pSh7xk9bYyhwwD", "created_ms": created})
    db.x("UPDATE audits SET ts=?", (now - 6900,))
    demo = [("DEMOa1111111111111111111111111111111111pump", "MOONCAT", 0.000412, 1.18, "open"),
            ("DEMOb2222222222222222222222222222222222pump", "ZAPZAP", 0.0000913, 0.965, "open"),
            ("DEMOc3333333333333333333333333333333333pump", "BLOOP", 0.00221, 1.27, "closed"),
            ("DEMOd4444444444444444444444444444444444pump", "RUGLESS", 0.0153, 0.93, "closed")]
    stack = 200.0
    for i, (m, sym, p0, mult, st) in enumerate(demo):
        opened = now - (5 - i) * 3600 * 0.9
        db.insert("coins", {"mint": m, "symbol": sym, "name": sym.title(), "status": "signalled",
                            "migrated_at": opened - 1200, "updated_at": opened})
        price = p0
        for k in range(160):
            ts = opened - 600 + k * 30
            drift = (mult - 1) * k / 160
            price = p0 * (1 + drift + 0.04 * math.sin(k / 6) + random.uniform(-0.02, 0.02))
            db.insert("prices", {"mint": m, "ts": ts, "price_usd": price, "liq_usd": 52000, "vol_m5": 4000})
        fee = 0.855
        qty = (47.5 - fee) / p0
        pid = db.insert("positions", {"mint": m, "mode": "PAPER", "opened_at": opened, "entry_price": p0, "ticket_usd": 47.5,
                                      "qty": qty, "entry_fee": fee, "peak_price": p0 * max(mult, 1.05), "status": "open"})
        stack = db.move_stack(-47.5, f"paper buy {sym} #{pid}")
        db.insert("signals", {"mint": m, "ts": opened, "side": "buy", "price_usd": p0, "ticket_usd": 47.5, "mode": "PAPER", "status": "filled_paper"})
        db.insert("activity", {"ts": opened - 900, "agent": "scanner", "kind": "pass", "mint": m, "text": "liq $52,400, age 14 min, top10 18.2% → Auditor"})
        db.insert("activity", {"ts": opened - 60, "agent": "auditor", "kind": "pass", "mint": m, "text": "8/8 layers pass"})
        db.insert("activity", {"ts": opened - 30, "agent": "analyst", "kind": "buy", "mint": m, "text": "alive 1.00, organic 0.88, chase 0.12, score 93"})
        db.insert("activity", {"ts": opened, "agent": "executor", "kind": "buy", "mint": m, "text": f"PAPER buy $47.50 (fee $0.86; bumped to min ticket)"})
        db.insert("audits", {"mint": m, "ts": opened - 60, "passed": 1, "layers": json.dumps(res["layers"]),
                             "bundled": res["bundled"], "summary": "8/8 layers pass"})
        db.insert("verdicts", {"mint": m, "ts": opened - 30, "mode": "rules", "alive": 1.0, "organic": 0.88, "chase": 0.12,
                               "score": 93, "flush": 0, "decision": "buy", "reasons": json.dumps(["alive 1.00, organic 0.88, chase 0.12, score 93"])})
        db.insert("scans", {"mint": m, "ts": opened - 900, "pool": "POOL" + sym, "dex_id": "pumpswap", "price_usd": p0, "liq_usd": 52400,
                            "vol_h1": 61000, "buys_h1": 260, "chg_m5": 2.1, "chg_h1": 9.6, "chg_h24": 40, "result": "pass", "reason": "demo"})
        if st == "closed":
            px = p0 * mult
            gross = qty * px
            xfee = 0.855 if gross > 47.5 else gross * 0.018
            pnl = gross - xfee - 47.5
            why = "trail: -12.1% off peak (+41%)" if mult > 1 else "stop -7.0% ≤ -6%"
            db.update("positions", "id=?", (pid,), {"status": "closed", "closed_at": opened + 3000, "exit_price": px, "exit_fee": xfee,
                                                     "pnl_usd": round(pnl, 4), "pnl_pct": round(pnl / 47.5 * 100, 2), "exit_reason": why})
            db.move_stack(gross - xfee, f"paper sell {sym}")
            db.insert("activity", {"ts": opened + 3000, "agent": "risk", "kind": "exit", "mint": m, "text": f"PAPER sell {sym}: {why} · P&L ${pnl:+.2f}"})
    for k, (sym, why) in enumerate([("PEPEX", "top10 41.3% > 25%"), ("SNEK", "liquidity $12,300 < $40,000"),
                                    ("WOOF", "already a runner: h24 +412% ≥ 300%")]):
        m = f"DEMOskip{k}111111111111111111111111111pump"
        db.insert("coins", {"mint": m, "symbol": sym, "status": "skipped", "status_reason": why, "migrated_at": now - 1800 * (k + 1)})
        db.insert("activity", {"ts": now - 1700 * (k + 1), "agent": "scanner", "kind": "skip", "mint": m, "text": why})
        db.skip(m, "scanner", why)
    db.insert("activity", {"ts": mint and now - 6900, "agent": "auditor", "kind": "pass", "mint": mint, "text": res["summary"]})
    db.insert("activity", {"ts": now - 6850, "agent": "analyst", "kind": "reject", "mint": mint, "text": "organic 0.50 < 0.65; flush: m5 -69.0% ≤ -5%"})
    db.insert("coins", {"mint": "DEMOwatch11111111111111111111111111111pump", "symbol": "FRESH", "status": "watching", "migrated_at": now - 240})
    db.insert("activity", {"ts": now - 240, "agent": "scanner", "kind": "watch", "mint": "DEMOwatch11111111111111111111111111111pump", "text": "new migration — watching"})
    for a in ("scanner", "auditor", "analyst", "executor", "risk"):
        db.heartbeat(a, "ok", "")
    return db


if __name__ == "__main__":
    asyncio.run(seed(sys.argv[1]))
    print("seeded", sys.argv[1])
