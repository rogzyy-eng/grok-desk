"""api.py — FastAPI + SSE (STEP 7). Every /api call needs APP_TOKEN (Authorization: Bearer) else 401.
The token is never returned and never read from a URL. Every coin carries links.coin_url."""
import asyncio
import hmac
import json
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import commands
import config
import links
from agents.risk import pos_cfg
import report
import telegram
import webpush
from agents.executor import skip_warning

RANGES = {"LIVE": 900, "1H": 3600, "4H": 4 * 3600, "1D": 86400, "1W": 7 * 86400, "ALL": None}


def make_app(desk):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    db = desk.db

    def auth(request: Request):
        tok = config.APP_TOKEN
        h = request.headers.get("authorization", "")
        given = h[7:] if h.lower().startswith("bearer ") else ""
        if not tok or not given or not hmac.compare_digest(given.encode(), tok.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")
        return True

    @app.middleware("http")
    async def headers(request, call_next):
        resp = await call_next(request)
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self'; connect-src 'self'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    A = [Depends(auth)]

    def last_price(mint):
        r = db.one("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC", (mint,))
        return r["price"] if r else None

    def pos_view(p):
        lp = p["last_price"] or last_price(p["mint"]) or p["entry_price"]
        c = db.one("SELECT symbol, name, chain FROM coins WHERE mint=?", (p["mint"],)) or {}
        stop, trail, exit_line, active = desk.risk_lines(p)
        return {**p, "symbol": c.get("symbol"), "name": c.get("name"), "ticket_usd": p["size_usd"],
                "value_usd": round((p["qty"] or 0) * lp, 4), "last": lp,
                "upnl_pct": round((lp / p["entry_price"] - 1) * 100, 2) if p["entry_price"] else None,
                "stop_line": stop, "trail_line": trail, "exit_line": exit_line,
                "timeout_left_s": max(0, p["opened_at"] + float(pos_cfg(p, db.section("risk"))["timeout_h"]) * 3600 - time.time()),
                "link": links.coin_url(p["mint"], c.get("chain") or "solana")}

    def agent_cards():
        out = []
        day = time.time() - 86400
        for name in config.AGENTS:
            hb = db.one("SELECT * FROM heartbeats WHERE agent=?", (name,))
            last = db.one("SELECT d.*, c.symbol FROM decisions d LEFT JOIN coins c USING(mint) WHERE agent=? ORDER BY d.id DESC", (name,))
            n = db.one("SELECT count(*) n FROM decisions WHERE agent=? AND ts>?", (name, day))["n"]
            card = {"name": name, "heartbeat": hb, "last": last and {**last, "text": last["reason"]}, "decisions_24h": n,
                    "paused": name in (db.get("desk.paused_agents") or [])}
            if name == "auditor":
                card.update(desk.auditor.stats())
            if name == "risk":
                card["guarding"] = bool(db.open_positions())
                card["pills"] = [f"stop -{db.get('risk.stop_pct'):g}%", f"trail {db.get('risk.trail_pct'):g}%"]
            out.append(card)
        return out

    @app.get("/api/state", dependencies=A)
    def state():
        day = time.time() - 86400
        opens = [pos_view(p) for p in db.open_positions()]
        bal = db.equity()
        start = db.get("desk.paper_start_stack", 200)
        b0 = db.one("SELECT value FROM balance WHERE ts>=? ORDER BY ts", (day,))
        today = round((bal / b0["value"] - 1) * 100, 2) if b0 and b0["value"] else 0.0
        curve = [[r["ts"], r["value"]] for r in db.q("SELECT ts, value FROM balance ORDER BY ts DESC LIMIT 300")][::-1]
        return {"mode": db.get("desk.mode"), "paused": bool(db.get("desk.paused_agents")),
                "paused_agents": db.get("desk.paused_agents") or [], "balance": round(bal, 2), "equity_usd": round(bal, 2),
                "today_pct": today, "multiplier": round(bal / start, 3) if start else None, "start_stack": start,
                "stack_usd": round(db.cash(), 2), "positions": opens, "agents": agent_cards(),
                "live_missing": commands.live_gate(db), "paper_nights": db.get("desk.paper_nights"),
                "notifier": db.get("desk.notifier"), "push_ready": webpush.enabled(), "layer8_proof": db.get("desk.layer8_proof"),
                "warning": skip_warning(db),
                "today": {"seen": db.one("SELECT count(*) n FROM coins WHERE migrated_at>?", (day,))["n"],
                          "audited": db.one("SELECT count(DISTINCT run_id) n FROM audits WHERE ts>?", (day,))["n"],
                          "signals": db.one("SELECT count(*) n FROM signals WHERE ts>? AND side='buy'", (day,))["n"]},
                "watching": db.q("SELECT mint, symbol, status FROM coins WHERE status IN ('waiting','auditing','analysing','sizing') "
                                 "ORDER BY migrated_at DESC LIMIT 20"),
                "equity_curve": curve or [[time.time(), bal]], "server_time": time.time()}

    @app.get("/api/chart", dependencies=A)
    def chart(range: str = "1D", mint: str = None, tf: int = 60):
        if mint:   # legacy candle call used by the Coin screen
            return candles(mint, tf)
        span = RANGES.get(range.upper(), 86400)
        since = 0 if span is None else time.time() - span
        pts = [[r["ts"], r["value"]] for r in db.q("SELECT ts, value FROM balance WHERE ts>=? ORDER BY ts", (since,))]
        marks = [{"ts": a["ts"], "kind": a["kind"], "title": a["title"], "value": a["value"]}
                 for a in db.q("SELECT * FROM activity WHERE ts>=? AND kind IN ('bought','sold','stopped') ORDER BY ts", (since,))]
        return {"range": range.upper(), "points": pts, "marks": marks}

    def candles(mint, tf=60, limit=120):
        tf = max(15, min(int(tf), 3600))
        rows = db.q("SELECT ts, price, volume_m5 FROM prices WHERE mint=? AND price IS NOT NULL ORDER BY ts", (mint,))
        cs, prev_v = {}, None
        for r in rows:
            b = int(r["ts"] // tf * tf)
            p = r["price"]
            dv = 0 if prev_v is None or r["volume_m5"] is None else abs(r["volume_m5"] - prev_v)   # volume bar = change of volume.m5
            prev_v = r["volume_m5"] if r["volume_m5"] is not None else prev_v
            c = cs.get(b)
            if not c:
                cs[b] = [b, p, p, p, p, dv]
            else:
                c[2] = max(c[2], p); c[3] = min(c[3], p); c[4] = p; c[5] += dv
        pos = db.one("SELECT * FROM positions WHERE mint=? ORDER BY id DESC", (mint,))
        return {"mint": mint, "tf": tf, "candles": sorted(cs.values())[-limit:],
                "entry": pos and {"entry_price": pos["entry_price"], "opened_at": pos["opened_at"]},
                "exit_line": pos and pos["status"] in ("open", "exiting") and pos["exit_line"]}

    @app.get("/api/activity", dependencies=A)
    def activity(before: float = None, limit: int = 30):
        sql, args = "SELECT a.*, c.symbol FROM activity a LEFT JOIN coins c USING(mint) WHERE 1=1", []
        if before:
            sql += " AND a.ts<?"; args.append(before)
        sql += " ORDER BY a.ts DESC LIMIT ?"; args.append(min(int(limit), 30))
        rows = db.q(sql, args)
        for r in rows:
            r["text"] = f"{r['subtitle']}" + (f" · {r['value']}" if r["value"] else "")
            if r.get("mint"):
                r["link"] = links.coin_url(r["mint"], "solana")
        return {"items": rows, "rows": rows}

    @app.get("/api/coin/{mint}", dependencies=A)
    def coin(mint: str):
        c = db.one("SELECT * FROM coins WHERE mint=?", (mint,))
        if not c:
            raise HTTPException(404, "not found")
        desk.app_coins[mint] = time.time()
        scan = db.one("SELECT * FROM scans WHERE mint=? ORDER BY ts DESC", (mint,)) or {}
        v = db.one("SELECT * FROM verdicts WHERE mint=? ORDER BY id DESC", (mint,))
        pos = db.one("SELECT * FROM positions WHERE mint=? ORDER BY id DESC", (mint,))
        run = db.one("SELECT run_id FROM audits WHERE mint=? ORDER BY id DESC", (mint,))
        shield = None
        if run:
            ok = db.one("SELECT sum(pass=1) n FROM audits WHERE run_id=?", (run["run_id"],))["n"] or 0
            shield = f"{ok}/8"
        compat_scan = {"price_usd": last_price(mint) or scan.get("price"), "chg_h1": scan.get("h1_change_pct"),
                       "chg_m5": scan.get("m5_change_pct"), "liq_usd": scan.get("liquidity_usd")}
        verdict = v and {**v, "decision": "buy" if v["pass"] else "reject", "alive": v["alive_6h"], "reasons": [v["reason"]]}
        return {"coin": c, "scan": compat_scan, "stats": {"market_cap": scan.get("mcap"), "liquidity": scan.get("liquidity_usd"),
                "volume_24h": scan.get("volume_h24"), "holders": c["holders"] if c["holders"] is not None else "n/a"},
                "shield": shield, "verdict": verdict, "position": pos_view(pos) if pos else None,
                "candles": candles(mint)["candles"], "coin_url": links.coin_url(mint, c["chain"] or "solana"),
                "link": links.coin_url(mint, c["chain"] or "solana"),
                "button": "Sell on FOMO" if pos and pos["status"] in ("open", "exiting") else "Buy on FOMO"}

    @app.get("/api/audit/{mint}", dependencies=A)
    def audit(mint: str):
        run = db.one("SELECT run_id, ts FROM audits WHERE mint=? ORDER BY id DESC", (mint,))
        if not run:
            raise HTTPException(404, "no audit")
        desk.app_coins[mint] = time.time()
        rows = db.q("SELECT * FROM audits WHERE run_id=? AND mint=? ORDER BY layer", (run["run_id"], mint))
        layers = [{"n": r["layer"], "name": r["name"], "value": r["value_text"], "source": r["source"], "pass": r["pass"],
                   "status": "pass" if r["pass"] == 1 else "fail" if r["pass"] == 0 else "notchecked", "note": r["value_text"]}
                  for r in rows]
        red = next((l for l in layers if l["status"] == "fail"), None)
        name = db.coin_name(mint)
        sig = db.one("SELECT size_usd FROM signals WHERE mint=? ORDER BY id DESC", (mint,))
        if red:
            head, sub = f"Rejected · {red['value']}", f"Grok Bot won't buy {name}"
        else:
            head, sub = "Cleared · 8/8 parts", f"Sent to Executor · ${(sig or {}).get('size_usd') or 0:.2f}"
        return {"mint": mint, "ts": run["ts"], "layers": layers, "passed": red is None, "headline": head, "subline": sub,
                "summary": head, "enabled": db.get("auditor.enabled"), "link": links.coin_url(mint, "solana")}

    @app.get("/api/agents", dependencies=A)
    def agents():
        return {"agents": agent_cards()}

    @app.get("/api/agents/{name}/decisions", dependencies=A)
    def decisions(name: str):
        if name not in config.AGENTS:
            raise HTTPException(404, "unknown agent")
        rows = db.q("SELECT d.ts, d.agent, d.mint, d.decision AS kind, d.reason AS text, c.symbol FROM decisions d "
                    "LEFT JOIN coins c USING(mint) WHERE agent=? ORDER BY d.id DESC LIMIT 20", (name,))
        for r in rows:
            if r["mint"]:
                r["link"] = links.coin_url(r["mint"], "solana")
        return {"items": rows}

    @app.get("/api/decisions", dependencies=A)
    def decisions_legacy(agent: str):
        return decisions(agent)

    @app.get("/api/account", dependencies=A)
    def account():
        h = db.get("desk.fomo_handle")
        closed = db.q("SELECT p.*, c.symbol FROM positions p LEFT JOIN coins c USING(mint) WHERE p.status='closed' "
                      "ORDER BY closed_at DESC LIMIT 100")
        ex = db.section("executor")
        return {"fomo_handle": h, "profile_url": links.profile_url(h), "signup_url": links.SIGNUP_URL,
                "mode": db.get("desk.mode"), "stack_usd": round(db.cash(), 2), "live_stack": db.get("desk.live_stack"),
                "balance": round(db.equity(), 2), "closed": closed,
                "fees_usd": round(sum(p["fees_usd"] or 0 for p in closed), 2),
                "min_stack_for_pct": config.min_stack(ex["min_ticket_usd"], ex["stack_pct"]),
                "live_missing": commands.live_gate(db), "paper_nights": db.get("desk.paper_nights"),
                "telegram": "connected" if telegram.configured() else "not connected", "push_public_key": webpush.public_key(),
                "warning": skip_warning(db)}

    @app.get("/api/settings", dependencies=A)
    def get_settings():
        s = {k: v for k, v in db.all_settings().items() if k in config.DEFAULTS}
        s = {sec: {k: v for k, v in vals.items() if k in config.DEFAULTS[sec]} for sec, vals in s.items()}
        return {"settings": s, "ranges": config.RANGES, "changes": db.q("SELECT * FROM change_log ORDER BY ts DESC LIMIT 50")}

    @app.post("/api/settings", dependencies=A)
    async def post_settings(request: Request):
        b = await request.json()
        who = "app-confirmed-twice" if b.get("confirm_twice") else "app"
        try:
            key, old, new = commands.set_setting(db, str(b.get("key")), b.get("value"), who)
        except (commands.GateError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "key": key, "old": old, "new": new, "warning": skip_warning(db)}

    @app.post("/api/pause", dependencies=A)
    async def pause(request: Request):
        b = await request.json() if (await request.body()) else {}
        return await commands.handle(desk, f"pause {b.get('agent', 'all')}")

    @app.post("/api/resume", dependencies=A)
    async def resume(request: Request):
        b = await request.json() if (await request.body()) else {}
        return await commands.handle(desk, f"resume {b.get('agent', 'all')}")

    @app.post("/api/mode", dependencies=A)
    async def mode(request: Request):
        b = await request.json()
        try:
            m = commands.set_mode(db, b.get("mode"), b.get("confirm", ""), "app")
        except commands.GateError as e:
            raise HTTPException(403, str(e))
        return {"ok": True, "mode": m}

    @app.get("/api/push", dependencies=A)
    @app.get("/api/push/key", dependencies=A)
    def push_key():
        return {"publicKey": webpush.public_key(), "enabled": webpush.enabled()}

    @app.post("/api/push", dependencies=A)
    @app.post("/api/push/subscribe", dependencies=A)
    async def push_sub(request: Request):
        try:
            webpush.save_subscription(db, await request.json())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/push/test", dependencies=A)
    async def push_test():
        h = db.get("desk.fomo_handle")
        rec = await desk.notifier.push("test", "Test push", buttons=[{"label": "Your FOMO profile", "url": links.profile_url(h)}])
        return {"ok": True, "result": rec["result"]}

    @app.post("/api/command", dependencies=A)
    async def command(request: Request):
        b = await request.json()
        return await commands.handle(desk, str(b.get("text", ""))[:300], "app")

    @app.get("/api/report", dependencies=A)
    def get_report():
        r = report.build(db)
        r["text"] = report.to_text(r)
        return r

    @app.post("/api/report/page", dependencies=A)
    async def set_page(request: Request):
        url = str((await request.json()).get("url", ""))
        if not url.startswith("https://"):
            raise HTTPException(400, "https url required")
        db.x("INSERT OR REPLACE INTO settings VALUES('desk.last_page_url',?,?)", (json.dumps(url), time.time()))
        return {"ok": True}

    @app.get("/api/health", dependencies=A)
    def health():
        s = commands.status(db)
        stale = [a for a, v in s["agents"].items() if v["age_s"] > 180]
        return {"ok": not stale, "stale": stale, **s, "layer8_proof": db.get("desk.layer8_proof")}

    @app.get("/api/stream", dependencies=A)
    async def stream(request: Request):
        q = desk.bus.subscribe_all()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                        yield f"event: {ev['topic']}\ndata: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                desk.bus.unsubscribe_all(q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/sw.js")
    def sw():
        return FileResponse(config.WEB_DIR / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
    return app
