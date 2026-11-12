"""AGENT 2 · AUDITOR — takes every coin apart, 8 layers, in order. First red stops the audit;
the rest are "not checked". 7 of 8 is a no. No data is never a pass."""
import asyncio
import json
import time

import config
import fields
from agents.base import Agent, iso
from notifier import buy_button
from sources import FetchError

NAMES = ["Mint authority", "Freeze authority", "Dev holding", "Top 10 holders", "LP locked",
         "Pool age", "Bundled wallets", "Delegations"]


def short(a):
    return f"{a[:4]}...{a[-3:]}" if a and len(a) > 8 else str(a)


class Red(Exception):
    def __init__(self, value, source):
        super().__init__(value)
        self.value, self.source = value, source


class Auditor(Agent):
    name = "auditor"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pending = {}
        self.layer8_alerted = False

    def stats(self):
        day = self.clock() - 86400
        runs = self.db.q("SELECT run_id, min(coalesce(pass,0)) ok, count(pass) n FROM audits WHERE ts>? GROUP BY run_id", (day,))
        return {"taken_apart": len(runs), "rejected": sum(1 for r in runs if not (r["ok"] and r["n"] == 8))}

    async def fetch(self, fn, *a, label):
        try:
            return await self.with_retries(fn, *a)
        except FetchError:
            raise Red(f"no data: {label}", "rpc" if label in ("mint", "delegations", "supply") else "rugcheck")

    async def audit(self, mint, scan, now=None):
        c = self.cfg()
        now = now or self.clock()
        coin = self.db.one("SELECT * FROM coins WHERE mint=?", (mint,)) or {}
        pool = scan.get("pool") or coin.get("pool")
        run_id = int(now * 1000) % 10**12
        layers, reason, holders = [], None, None
        ctx = {}

        async def L1():
            ctx["mi"] = fields.rpc_mint_info(await self.fetch(self.src.mint_info, mint, label="mint"))
            if not ctx["mi"]["found"]:
                raise Red("no data: mint", "rpc")
            if ctx["mi"]["mint_authority"] is not None:
                raise Red(f"mint authority live ({short(ctx['mi']['mint_authority'])})", "rpc")
            return "revoked", "rpc"

        async def L2():
            if ctx["mi"]["freeze_authority"] is not None:
                raise Red(f"freeze authority live ({short(ctx['mi']['freeze_authority'])})", "rpc")
            return "revoked", "rpc"

        async def L3():
            supply = ctx["mi"]["supply"]
            if not supply:
                raise Red("no supply", "rpc")
            ctx["rep"] = await self.fetch(self.src.rc_report, mint, label="dev")
            creator = coin.get("creator") or fields.rc_creator(ctx["rep"])
            if not creator:
                raise Red("creator unknown", "rugcheck")
            dev = round(sum(h["pct"] or 0 for h in fields.rc_top_holders(ctx["rep"]) if h["owner"] == creator), 1)
            then, now_bal = coin.get("creator_balance_at_migration"), fields.rc_creator_balance(ctx["rep"])
            sold = 0 if then is None or now_bal is None else max(0.0, then - now_bal)
            if dev > c["max_dev_pct"]:
                raise Red(f"dev holds {dev:g}%", "rugcheck")
            if sold > 0:
                raise Red(f"dev sold {100 * sold / supply:.1f}%", "rugcheck")
            ctx["dev"] = dev
            return f"dev holds {dev:g}%, sold 0", "rugcheck"

        async def L4():
            t10 = scan.get("top10_pct")
            if t10 is None:
                vaults = fields.rc_pool_vaults(ctx["rep"])
                pools = fields.rc_pool_pubkeys(ctx["rep"])
                hs = [h for h in fields.rc_top_holders(ctx["rep"]) if h["owner"] not in pools and h["address"] not in vaults]
                if not hs:
                    raise Red("no data: top 10", "rugcheck")
                t10 = round(sum(h["pct"] or 0 for h in hs[:10]), 1)
            ctx["top10"] = t10
            if t10 > c["max_top10_pct"]:
                raise Red(f"top 10 hold {t10:.0f}%", "rugcheck")
            return f"top 10 hold {t10:.0f}%", "rugcheck"

        async def L5():
            lp = fields.rc_lp_locked_pct(ctx["rep"], pool)   # markets[].lp.lpLockedPct of the real pool
            if lp is None:
                raise Red("no data: LP", "rugcheck")
            ctx["lp"] = lp
            if lp < c["min_lp_locked_pct"]:
                raise Red(f"LP {lp:.0f}% locked", "rugcheck")
            return f"LP {lp:.0f}% locked", "rugcheck"

        async def L6():
            created = scan.get("created_ms")
            if not created:
                raise Red("no data: age", "dexscreener")
            age = (now - created / 1000) / 60
            if age < c["min_pool_age_min"]:
                raise Red("pool too young", "dexscreener")
            return f"{age:.0f} min", "dexscreener"

        async def L7():
            try:   # decisions.md: refetched at age >= 15 min; [] -> None; error -> red "no data: bundles"
                g = await self.with_retries(self.src.rc_graph, mint)
            except FetchError:
                raise Red("no data: bundles", "rugcheck")
            n = fields.graph_bundled(g)
            ctx["bundled"] = n
            if (n or 0) >= c["max_bundled"]:
                raise Red(f"{n} bundled wallets", "rugcheck")
            return ("None" if not n else f"{n} bundled wallets"), "rugcheck"

        async def L8():
            accts = []
            try:
                for prog in (config.TOKEN_PROGRAM, config.TOKEN_2022_PROGRAM):
                    accts += fields.rpc_token_accounts(await self.with_retries(self.src.token_accounts, prog, mint))
            except FetchError:
                await self.layer8_down()
                raise Red("no data: delegations", "rpc")
            nonlocal holders
            holders = sum(1 for a in accts if a["amount"] > 0)
            self.db.update("coins", "mint=?", (mint,), {"holders": holders})
            groups = fields.delegations([a for a in accts if a["amount"] > 0])
            ctx["delegations"] = groups
            if groups:
                d, n = max(groups.items(), key=lambda kv: kv[1])
                if n >= c["max_delegated_accounts"]:
                    raise Red(f"{n} wallets → {short(d)}", "rpc")
                return f"max {n} per delegate", "rpc"
            return "None", "rpc"

        steps = [L1, L2, L3, L4, L5, L6, L7, L8]
        for i, step in enumerate(steps, 1):
            t0 = time.perf_counter()
            if reason is None:
                try:
                    value, source = await step()
                    ok = True
                except Red as r:
                    value, source, ok = r.value, r.source, False
                    reason = r.value
            else:
                value, source, ok = "not checked", None, None
            ms = int((time.perf_counter() - t0) * 1000)
            row = {"n": i, "name": NAMES[i - 1], "pass": ok, "value": value, "source": source}
            layers.append(row)
            self.db.insert("audits", {"mint": mint, "ts": now, "layer": i, "name": NAMES[i - 1], "value_text": value,
                                      "pass": None if ok is None else int(ok), "source": source, "run_id": run_id, "ms": ms})
            self.bus.publish("audit_layer", {"mint": mint, "run_id": run_id, **row})
            self.log.info("%s L%d %s pass=%s value=%r ms=%d", mint, i, NAMES[i - 1], ok, value, ms)
        passed = reason is None
        out = {"agent": "auditor", "mint": mint, "ts": iso(now), "passed": passed,
               "layersChecked": sum(1 for l in layers if l["pass"] is not None), "layers": layers,
               "holders": holders, "reason": reason, "bundled": ctx.get("bundled"), "dev": ctx.get("dev"),
               "top10": ctx.get("top10"), "lp": ctx.get("lp"), "delegations": ctx.get("delegations")}
        self.log.info("%s verdict passed=%s reason=%s", mint, passed, reason)
        return out

    async def layer8_down(self):
        self.db.set("desk.layer8_down_kills", int(self.db.get("desk.layer8_down_kills", 0) or 0) + 1, "auditor")
        if not self.layer8_alerted:
            self.layer8_alerted = True
            await self.notifier.push("desk", "layer 8 down, every coin is red until rpc_url changes", force=True)

    def rejected_tonight(self, mint):
        return self.db.one("SELECT 1 FROM audits WHERE mint=? AND pass=0 AND ts>?", (mint, self.clock() - 18 * 3600))

    async def on_event(self, ev):
        self.pending[ev["mint"]] = ev

    async def tick(self):
        now = self.clock()
        for mint, ev in list(self.pending.items()):
            created = ev.get("createdMs") or (ev.get("scan") or {}).get("created_ms")
            age = (now - created / 1000) / 60 if created else 1e9
            if age < self.db.get("auditor.graph_min_age_min", 15):
                continue
            self.pending.pop(mint)
            await self.process(ev)

    async def process(self, ev):
        mint = ev["mint"]
        if self.rejected_tonight(mint):
            self.decide(mint, "skip", "already rejected tonight")
            return None
        name = self.db.coin_name(mint)
        if not self.db.get("auditor.enabled", True):
            out = {"agent": "auditor", "mint": mint, "passed": True, "layersChecked": 0, "layers": [], "reason": None}
        else:
            out = await self.audit(mint, ev.get("scan") or {})
        if out["passed"]:
            self.db.update("coins", "mint=?", (mint,), {"status": "analysing"})
            self.decide(mint, "pass", "8/8 → Executor" if out["layers"] else "auditor off", out)
            self.bus.publish("analyst", {"mint": mint, "scanner": ev, "auditor": out})
        else:
            self.db.update("coins", "mint=?", (mint,), {"status": "rejected", "status_reason": out["reason"]})
            self.db.skip(mint, self.name, out["reason"])
            self.decide(mint, "reject", out["reason"], out)
            self.bus.activity("rejected", mint, "auditor", f"Rejected {name}", f"Auditor · {out['reason']}", "Skipped")
            await self.notifier.push("rejected", f"Rejected {name} · {out['reason']}", mint=mint, buttons=[buy_button(mint)])
        return out

    async def run(self):
        await asyncio.gather(self.queue_loop("auditor", self.on_event), self.loop("auditor.tick_s", self.tick))
