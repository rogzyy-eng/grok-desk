"""report.py — data for the morning Page (the 'night-report' routine on Grok Bot reads /api/report)."""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def window(db, now=None):
    tz = ZoneInfo(db.get("desk.tz", "Europe/Istanbul"))
    now = datetime.fromtimestamp(now or time.time(), tz)
    hh, mm = map(int, str(db.get("desk.report_time", "08:00")).split(":"))
    end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end > now:
        end -= timedelta(days=1)
    return (end - timedelta(days=1)).timestamp(), end.timestamp()


def build(db, start=None, end=None):
    if start is None:
        start, end = window(db)
    a = (start, end or time.time())
    closed = db.q("SELECT p.*, c.symbol FROM positions p LEFT JOIN coins c USING(mint) "
                  "WHERE p.status='closed' AND p.closed_at BETWEEN ? AND ?", a)
    wins = [p for p in closed if (p["pnl_usd"] or 0) > 0]
    skips = {}
    for s in db.q("SELECT agent FROM skips WHERE ts BETWEEN ? AND ?", a):
        skips[s["agent"]] = skips.get(s["agent"], 0) + 1
    return {"from": a[0], "to": a[1], "mode": db.get("desk.mode"),
            "layer8_down_kills": db.get("desk.layer8_down_kills", 0),   # on top of the Page (Auditor edge case)
            "coins_seen": db.one("SELECT count(*) n FROM coins WHERE migrated_at BETWEEN ? AND ?", a)["n"],
            "trades": [{"symbol": p["symbol"], "mint": p["mint"], "pnl_usd": p["pnl_usd"], "reason": p["exit_reason"]} for p in closed],
            "wins": len(wins), "losses": len(closed) - len(wins),
            "pnl_usd": round(sum(p["pnl_usd"] or 0 for p in closed), 2),
            "fees_usd": round(sum(p["fees_usd"] or 0 for p in closed), 2),
            "open_positions": len(db.open_positions()), "balance": round(db.equity(), 2),
            "skips_by_agent": skips, "paper_nights": db.get("desk.paper_nights")}


def to_text(r):
    head = f"⚠ layer 8 down: {r['layer8_down_kills']} coins killed\n" if r.get("layer8_down_kills") else ""
    return head + (f"Night ({r['mode']}): {len(r['trades'])} trades, {r['wins']}W/{r['losses']}L, P&L ${r['pnl_usd']:+.2f}, "
                   f"fees ${r['fees_usd']:.2f}, balance ${r['balance']:.2f}, coins seen {r['coins_seen']}")


def close_night(db, now=None):
    start, end = window(db, now)
    r = build(db, start, end)
    since = db.get("desk.paper_since")
    proof = (db.get("desk.layer8_proof") or {}).get("passed")
    full = db.get("desk.mode") == "PAPER" and since is not None and since <= start + 60 and bool(proof)
    if full and db.get("desk.last_night_closed") != end:
        db.set("desk.paper_nights", int(db.get("desk.paper_nights", 0) or 0) + 1, "report")
        db.set("desk.last_night_closed", end, "report")
    db.set("desk.layer8_down_kills", 0, "report")
    r["full_paper_night"] = bool(full)
    return r
