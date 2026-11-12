"""AGENT 1 · SCANNER — counts and cuts. Owns every coin that graduates off the pumpfun curve onto
PumpSwap. ONE PumpPortal connection (subscribeMigration only). Kill on the first hit, in order:
liquidity · crashed (h1/m5 drop, far under its recorded high) · top 10 · h1 change · copycat. Never changes a setting."""
import asyncio
import json
import re

import fields
from sources import FetchError
from agents.base import Agent, iso, money


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Scanner(Agent):
    name = "scanner"

    # ---- input: PumpPortal ----
    async def ws_loop(self):
        import websockets
        import config
        backoff = 2
        while not self.stop.is_set():
            try:
                async with websockets.connect(config.PUMPPORTAL_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    backoff = 2
                    self.log.info("pumpportal connected, subscribeMigration sent")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if not self.paused():
                            await self.add_coin(msg)
            except Exception as e:
                self.beat("warn", f"pumpportal {type(e).__name__}")
                await self.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def add_coin(self, msg):
        mig = fields.migration(msg)
        if not mig or self.db.one("SELECT 1 FROM coins WHERE mint=?", (mig["mint"],)):
            return None
        now = self.clock()
        creator, bal = mig["creator"], None
        try:   # creator null in the migration message -> RugCheck report 'creator' (decisions.md)
            rep = await self.with_retries(self.src.rc_report, mig["mint"])
            creator = creator or fields.rc_creator(rep)
            bal = fields.rc_creator_balance(rep)
        except Exception:
            pass
        self.db.insert("coins", {"mint": mig["mint"], "signature": mig["signature"], "pool_label": mig["pool_label"],
                                 "migrated_at": now, "creator": creator, "creator_balance_at_migration": bal,
                                 "status": "waiting", "updated_at": now})
        self.decide(mig["mint"], "waiting", f"graduated; creator {creator or 'unknown'}")
        self.log.info("waiting room + %s creator=%s balance=%s", mig["mint"], creator, bal)
        return mig["mint"]

    # ---- top 10 (pool and curve excluded) ----
    async def top10(self, mint, report):
        vaults = fields.rc_pool_vaults(report or {})
        try:
            supply = fields.rpc_supply(await self.with_retries(self.src.token_supply, mint))
            largest = fields.rpc_largest(await self.src.largest_accounts(mint))
            hs = [a for a in largest if a["address"] not in vaults]
            if supply and hs:
                return round(100 * sum(a["amount"] for a in hs[:10]) / supply, 2), "rpc"
        except Exception:
            pass
        if report:   # fallback: RugCheck topHolders, pool-owned accounts excluded
            pools = fields.rc_pool_pubkeys(report)
            hs = [h for h in fields.rc_top_holders(report) if h["owner"] not in pools and h["address"] not in vaults]
            if hs:
                return round(sum(h["pct"] or 0 for h in hs[:10]), 2), "rugcheck"
        return None, None

    def runners_today(self, mint):
        day = self.clock() - 86400
        return self.db.q("SELECT mint, name, symbol FROM coins WHERE mint<>? AND max_h24_pct>=? AND updated_at>?",
                         (mint, self.cfg()["runner_h24_pct"], day))

    async def tick(self):
        coins = [c for c in self.db.q("SELECT * FROM coins WHERE status='waiting'") if not self.db.skipped(c["mint"], self.clock())]
        for i in range(0, len(coins), 30):
            batch = coins[i:i + 30]
            try:
                pairs = await self.src.dex_pairs([c["mint"] for c in batch])   # 1 batch per tick
            except FetchError as e:
                if e.status == 429:
                    await self.backoff_429()
                    return
                self.log.warning("dexscreener batch failed: %s", e)
                continue
            except Exception as e:
                self.log.warning("dexscreener batch failed: %s", e)
                continue
            for coin in batch:
                mine = [p for p in (pairs or []) if isinstance(p, dict) and
                        (p.get("baseToken") or {}).get("address", coin["mint"]) == coin["mint"]]
                await self.evaluate(coin, mine)

    async def evaluate(self, coin, pairs):
        c = self.cfg()
        mint, now = coin["mint"], self.clock()
        best = fields.best_pair(pairs)
        waited = (now - (coin["migrated_at"] or now)) / 60
        if best is None:
            if waited > c["max_wait_min"]:
                return self.kill(coin, f"no PumpSwap pool after {c['max_wait_min']} min", None)
            return "wait"
        self.db.insert("prices", {"mint": mint, "ts": now, "price": best["price_usd"], "volume_m5": best["vol_m5"],
                                  "liquidity_usd": best["liq_usd"]})
        h24 = max(coin["max_h24_pct"] or -1e9, best["chg_h24"] if best["chg_h24"] is not None else -1e9)
        self.db.update("coins", "mint=?", (mint,), {"pool": best["pair_address"], "symbol": best["symbol"],
                       "name": best["name"], "max_h24_pct": h24, "updated_at": now})
        age = (now - best["created_ms"] / 1000) / 60 if best["created_ms"] else None
        scan = {"mint": mint, "ts": now, "liquidity_usd": best["liq_usd"], "h1_change_pct": best["chg_h1"],
                "age_min": age, "price": best["price_usd"], "mcap": best["mcap"], "volume_m5": best["vol_m5"],
                "volume_h1": best["vol_h1"], "volume_h24": best["vol_h24"], "buys_m5": best["buys_m5"],
                "sells_m5": best["sells_m5"], "buys_h1": best["buys_h1"], "sells_h1": best["sells_h1"],
                "m5_change_pct": best["chg_m5"], "h24_change_pct": best["chg_h24"], "pool": best["pair_address"],
                "created_ms": best["created_ms"]}
        if age is None or age < c["min_pool_age_min"]:
            if waited > c["max_wait_min"]:
                return self.kill(coin, f"pool never reached {c['min_pool_age_min']} min", scan)
            self.db.insert("scans", {**scan, "verdict": "wait", "reason": f"pool {age or 0:.0f} min old"})
            return "wait"
        # kill on the first hit, in this order
        if (best["liq_usd"] or 0) < c["min_liquidity_usd"]:
            return self.kill(coin, f"liq ${money(best['liq_usd'])}", scan)
        crash = self.crashed(mint, best, c)
        if crash:
            return self.kill(coin, crash, scan)
        report = None
        try:
            report = await self.with_retries(self.src.rc_report, mint)
        except Exception:
            pass
        t10, src = await self.top10(mint, report)
        scan["top10_pct"] = t10
        if t10 is None:
            return self.kill(coin, "no data: top 10", scan)
        if t10 > c["max_top10_pct"]:
            return self.kill(coin, f"top 10 hold {t10:.0f}%", scan)
        if best["chg_h1"] is not None and best["chg_h1"] > c["max_h1_change_pct"]:
            return self.kill(coin, f"+{best['chg_h1']:.0f}% in 1h", scan)
        for r in self.runners_today(mint):
            if (norm(best["name"]) and norm(best["name"]) == norm(r["name"])) or \
               (norm(best["symbol"]) and norm(best["symbol"]) == norm(r["symbol"])):
                return self.kill(coin, f"copycat of {r['symbol'] or r['name']}", scan)
        self.db.insert("scans", {**scan, "verdict": "pass", "reason": "→ Auditor"})
        self.db.update("coins", "mint=?", (mint,), {"status": "auditing"})
        out = {"agent": "scanner", "mint": mint, "name": best["name"], "symbol": best["symbol"], "ts": iso(now),
               "pool": best["pair_address"], "ageMin": round(age, 1), "liquidityUsd": best["liq_usd"],
               "top10Pct": t10, "top10Source": src, "h1ChangePct": best["chg_h1"], "priceUsd": best["price_usd"],
               "marketCap": best["mcap"], "createdMs": best["created_ms"], "scan": scan}
        self.decide(mint, "pass", f"liq ${money(best['liq_usd'])} · top 10 {t10:.0f}% · {age:.0f} min → Auditor", out)
        self.log.info("pass %s %s", mint, json.dumps({k: v for k, v in out.items() if k != "scan"}))
        self.bus.publish("auditor", out)
        return "pass"

    def crashed(self, mint, best, c):
        """Crashed coins never reach the Auditor: a -50% hour, a -40% 5 minutes, or >60% under the
        highest price this desk ever recorded for the mint (prices table)."""
        h1, m5, px = best.get("chg_h1"), best.get("chg_m5"), best.get("price_usd")
        if h1 is not None and h1 <= -abs(c.get("max_h1_drop_pct", 50)):
            return f"crashed {h1:.0f}% in 1h"
        if m5 is not None and m5 <= -abs(c.get("max_m5_drop_pct", 40)):
            return f"crashed {m5:.0f}% in 5 min"
        hi = (self.db.one("SELECT max(price) hi FROM prices WHERE mint=?", (mint,)) or {}).get("hi")
        if hi and px and px < hi * (1 - c.get("max_from_high_pct", 60) / 100):
            return f"crashed {(px / hi - 1) * 100:.0f}% from its high"
        return None

    def kill(self, coin, reason, scan):
        if scan:
            self.db.insert("scans", {**scan, "verdict": "kill", "reason": reason})
        self.db.update("coins", "mint=?", (coin["mint"],), {"status": "skipped", "status_reason": reason})
        self.db.skip(coin["mint"], self.name, reason)
        self.decide(coin["mint"], "kill", reason)
        self.log.info("kill %s %s", coin["mint"], reason)
        return "kill"

    async def run(self):
        await asyncio.gather(self.ws_loop(), self.loop("scanner.poll_s", self.tick))
