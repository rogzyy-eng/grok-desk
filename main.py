"""main.py — ONE process: 5 agents + API. PAPER by default. python main.py"""
import asyncio
import json
import logging
import time

import config
from bus import Bus
from db import DB
from notifier import Notifier

log = logging.getLogger("desk")


class Desk:
    def __init__(self, db=None, src=None, clock=time.time):
        from agents.analyst import Analyst
        from agents.auditor import Auditor
        from agents.executor import Executor
        from agents.risk import Risk
        from agents.scanner import Scanner
        self.db = db or DB()
        self.bus = Bus(self.db)
        self.notifier = Notifier(self.db, self.bus)
        if src is None:
            from sources import Sources
            src = Sources(self.db.get("desk.rpc_url"))
        self.src = src
        a = (self.db, self.bus, self.src, self.notifier)
        self.scanner, self.auditor, self.analyst = Scanner(*a, clock=clock), Auditor(*a, clock=clock), Analyst(*a, clock=clock)
        self.executor, self.risk = Executor(*a, clock=clock), Risk(*a, clock=clock)
        self.agents = [self.scanner, self.auditor, self.analyst, self.executor, self.risk]
        self.crashed = []
        self.app_coins = {}
        from agents.risk import lines
        self.risk_lines = lambda p: lines(p, self.db.section("risk")) if p.get("stop_pct") is not None else (None, None, None, False)   # mints opened in the app tonight (api.py price writer)
        if self.db.get("desk.mode") == "PAPER" and self.db.get("desk.paper_since") is None:
            self.db.set("desk.paper_since", time.time(), "startup")


async def layer8_proof(desk):
    """STEP 2 LAYER 8 PROOF: getProgramAccounts on Token AND Token-2022 for the sample mint must work
    before any PAPER night. While it fails, every agent except Risk stays paused."""
    from fields import rpc_token_accounts
    try:
        n = {}
        for prog in (config.TOKEN_PROGRAM, config.TOKEN_2022_PROGRAM):
            n[prog] = rpc_token_accounts(await desk.src.token_accounts(prog, config.LAYER8_PROOF_MINT))
        accts = n[config.TOKEN_2022_PROGRAM]
        res = {"passed": len(accts) > 0, "ts": time.time(), "token": len(n[config.TOKEN_PROGRAM]),
               "token2022": len(accts), "holders": sum(1 for a in accts if a["amount"] > 0)}
    except Exception as e:
        res = {"passed": False, "ts": time.time(), "error": str(e) or type(e).__name__}
    desk.db.x("INSERT INTO settings VALUES('desk.layer8_proof',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (json.dumps(res), time.time()))
    log.info("LAYER 8 proof %s", res)
    return res


async def proof_loop(desk):
    while True:
        res = await layer8_proof(desk)
        held = ["scanner", "auditor", "analyst", "executor"]
        cur = set(desk.db.get("desk.paused_agents") or [])
        if res["passed"]:
            if desk.db.get("desk.paused_by_proof"):
                desk.db.set("desk.paused_agents", sorted(cur - set(held)), "layer8_proof")
                desk.db.x("DELETE FROM settings WHERE key='desk.paused_by_proof'")
            return
        if not desk.db.get("desk.paused_by_proof"):
            desk.db.set("desk.paused_agents", sorted(cur | set(held)), "layer8_proof")
            desk.db.x("INSERT OR REPLACE INTO settings VALUES('desk.paused_by_proof','true',?)", (time.time(),))
        await asyncio.sleep(600)


async def minute_jobs(desk):
    """balance every minute (Home chart) + api.py price writer every 60 s + night close at report_time."""
    import report
    import fields
    from datetime import datetime
    from zoneinfo import ZoneInfo
    last_night = None
    while True:
        try:
            desk.db.snapshot_balance()
            covered = {c["mint"] for c in desk.db.q("SELECT mint FROM coins WHERE status='waiting'")}
            covered |= {p["mint"] for p in desk.db.open_positions()}
            since = time.time() - 18 * 3600
            want = {r["mint"] for r in desk.db.q("SELECT DISTINCT mint FROM signals WHERE ts>? UNION "
                                                  "SELECT DISTINCT mint FROM audits WHERE ts>?", (since, since))}
            want |= {m for m, t in desk.app_coins.items() if t > since}
            todo = sorted(want - covered)[:30]
            if todo:
                pairs = await desk.src.dex_pairs(todo)
                for m in todo:
                    b = fields.best_pair([x for x in pairs or [] if (x.get("baseToken") or {}).get("address", m) == m])
                    if b and b["price_usd"]:
                        desk.db.insert("prices", {"mint": m, "ts": time.time(), "price": b["price_usd"],
                                                  "volume_m5": b["vol_m5"], "liquidity_usd": b["liq_usd"]})
            now = datetime.now(ZoneInfo(desk.db.get("desk.tz", "Europe/Istanbul")))
            if now.strftime("%H:%M") == desk.db.get("desk.report_time", "08:00") and last_night != now.date():
                last_night = now.date()
                r = report.close_night(desk.db)
                await desk.notifier.push("report", report.to_text(r).splitlines()[0])
        except Exception as e:
            log.warning("minute job: %s", type(e).__name__)
        await asyncio.sleep(60)


async def supervise(desk, agent):
    while True:
        try:
            await agent.run()
            return
        except Exception as e:
            log.exception("%s crashed; restart in 5 s", agent.name)
            desk.db.heartbeat(agent.name, "error", f"crashed {type(e).__name__}")
            await asyncio.sleep(5)


async def amain():
    import uvicorn
    import commands
    import logs_setup
    import telegram
    from api import make_app
    logs_setup.setup()
    if not config.APP_TOKEN:
        raise SystemExit("APP_TOKEN missing — run deploy/install.sh (it writes /etc/grok-desk/desk.env)")
    desk = Desk()
    server = uvicorn.Server(uvicorn.Config(make_app(desk), host=config.HOST, port=config.PORT, access_log=False,
                                           log_level="warning", proxy_headers=True))
    tasks = [asyncio.create_task(server.serve()), asyncio.create_task(proof_loop(desk)),
             asyncio.create_task(minute_jobs(desk))]
    tasks += [asyncio.create_task(supervise(desk, a)) for a in desk.agents]
    stop = asyncio.Event()
    tasks.append(asyncio.create_task(telegram.poll_commands(desk.db, lambda t, source: commands.handle(desk, t, source), stop)))
    log.info("desk started in %s mode", desk.db.get("desk.mode"))
    await tasks[0]
    if not server.started:
        raise SystemExit("API server could not start (port busy?)")


if __name__ == "__main__":
    asyncio.run(amain())
