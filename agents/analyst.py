"""AGENT 3 · ANALYST — never decides itself. Builds a numbers-only STATE, asks Jev 4 closed
questions (or, with Jev skipped, the rules mode answers them from DexScreener numbers), and code
applies the GATE. score = round(100 * (alive_6h + organic + flush + (1 - chase)) / 4)."""
import config
import fields
from agents.base import Agent, iso
from notifier import buy_button

QUESTIONS = {
    "alive_6h": {"type": "noul", "instructions": "This coin is still actively traded 6 hours from now"},
    "organic": {"type": "noul", "instructions": "Most buyers in the last 30 minutes are separate real wallets"},
    "flush": {"type": "noul", "instructions": "Price just flushed down and buyers are absorbing it"},
    "chase": {"type": "noul", "instructions": "This entry is chasing a green candle that already ran"},
}


def fmt(v, d=0):
    return "n/a" if v is None else f"{v:,.{d}f}"


def build_state(name, age, p, au, max_chars=3000):
    lp = au.get("lp")
    bund = au.get("bundled")
    dl = au.get("delegations") or {}
    s = (f"Coin {name} on Solana, graduated {fmt(age)} min ago. Liquidity ${fmt(p.get('liq_usd'))}. "
         f"Market cap ${fmt(p.get('mcap'))}.\n"
         f"Last 5 min: {fmt(p.get('buys_m5'))} buys, {fmt(p.get('sells_m5'))} sells, volume ${fmt(p.get('vol_m5'))}, "
         f"price {fmt(p.get('chg_m5'), 1)}%.\n"
         f"Last 1h: {fmt(p.get('buys_h1'))} buys, {fmt(p.get('sells_h1'))} sells, volume ${fmt(p.get('vol_h1'))}, "
         f"price {fmt(p.get('chg_h1'), 1)}%.\n"
         f"Top 10 hold {fmt(au.get('top10'), 1)}%. Dev holds {fmt(au.get('dev'), 1)}%, sold 0. "
         f"LP {'n/a' if lp is None else f'{lp:.0f}% locked'}. Bundled {bund if bund else 'None'}. "
         f"Delegations {max(dl.values()) if dl else 'None'}.")
    return s[:max_chars]


def rules_answers(p, bundled, c):
    """Rules mode (Jev skipped): same 4 answers, 0..1, from fake-able DexScreener numbers.
    alive/organic read the 5-min window (a snipe lives in minutes; the 1h window was mostly history).
    flush = a -5%..-30% 5-min dip with buys > sells (a deeper drop is a dump, not a flush).
    chase = run-up over the hour; a crash (h1 < -rules_crash_h1_pct) is penalised as fully as a chase."""
    alive = min(1.0, (p.get("vol_m5") or 0) / c.get("rules_alive_volume_m5", 2000))
    organic = min(1.0, (p.get("buys_m5") or 0) / c.get("rules_organic_buys_m5", 12))
    if (bundled or 0) >= c["rules_bundled_from"]:
        organic *= c["rules_bundled_penalty"]
    m5 = p.get("chg_m5") or 0
    lo, hi = abs(c["rules_flush_pct"]), abs(c.get("rules_flush_max_pct", 30))
    flush = 1.0 if -hi <= m5 <= -lo and (p.get("buys_m5") or 0) > (p.get("sells_m5") or 0) else 0.0
    h1 = p.get("chg_h1") or 0
    if h1 < -abs(c.get("rules_crash_h1_pct", 50)):
        chase = 1.0
    else:
        chase = min(1.0, max(0.0, h1) / c["rules_chase_h1_pct"])
    return {"alive_6h": round(alive, 2), "organic": round(organic, 2), "flush": round(flush, 2), "chase": round(chase, 2)}


def m5_cap_reason(m5, cap):
    """Momentum cap: a coin already up more than [analyst.max_m5_change_pct] in 5 min is the top, not the
    start (2026-11-12: every buy at m5 +190..234% lost). -> Turkish reason or None."""
    if m5 is None or cap is None:
        return None
    m5, cap = float(m5), float(cap)
    return f"zirve kovalama: 5dk +%{m5:.0f} > %{cap:g}" if m5 > cap else None


def gate(a, c):
    score = round(100 * (a["alive_6h"] + a["organic"] + a["flush"] + (1 - a["chase"])) / 4)
    failed = []
    if a["alive_6h"] < c["alive_min"]:
        failed.append(f"alive_6h {a['alive_6h']:.2f}")
    if a["organic"] < c["organic_min"]:
        failed.append(f"organic {a['organic']:.2f}")
    if a["chase"] > c["chase_max"]:
        failed.append(f"chase {a['chase']:.2f}")
    if score < c["score_min"]:
        failed.append(f"score {score}")
    return score, failed


class Analyst(Agent):
    name = "analyst"

    def label(self, mode):
        return "Analyst (Jev)" if mode == "jev" else "Analyst (rules)"

    async def ask_jev(self, state, c):
        body = {"model": "jev-latest", "state": state, "questions": QUESTIONS}
        r = await self.src.jev(config.TYPESAFE_API_KEY, body, c["timeout_s"])
        ans = {k: float(r["answers"][k]["noul"]) for k in QUESTIONS}
        usage = r.get("usage") or {}
        return ans, r.get("model"), usage.get("input_tokens") or usage.get("inputTokens")

    async def judge(self, mint, pair, auditor_out, age=None):
        c = self.cfg()
        name = self.db.coin_name(mint)
        state = build_state(name, age, pair, auditor_out, c["state_max_chars"])
        mode, model, tokens = "rules", "rules", 0
        if c.get("mode") == "jev" and config.TYPESAFE_API_KEY:
            try:
                ans, model, tokens = await self.ask_jev(state, c)
                mode = "jev"
            except Exception as e:
                self.log.warning("Jev failed (%s) -> rules", e)
                ans = rules_answers(pair, auditor_out.get("bundled"), c)
        else:
            ans = rules_answers(pair, auditor_out.get("bundled"), c)
        score, failed = gate(ans, c)
        capped = m5_cap_reason(pair.get("chg_m5"), c.get("max_m5_change_pct", 100))
        if capped:
            failed.insert(0, capped)
        passed = not failed
        out = {"agent": "analyst", "mint": mint, "answers": ans, "score": score, "passed": passed, "failed": failed,
               "model": model, "inputTokens": tokens, "mode": mode, "label": self.label(mode), "state": state, "ts": iso(self.clock())}
        self.db.insert("verdicts", {"mint": mint, "ts": self.clock(), "alive_6h": ans["alive_6h"], "organic": ans["organic"],
                                    "flush": ans["flush"], "chase": ans["chase"], "score": score, "pass": int(passed),
                                    "reason": failed[0] if failed else "gate passed", "input_tokens": tokens,
                                    "mode": mode, "model": model})
        self.log.info("%s %s answers=%s score=%s failed=%s", mint, out["label"], ans, score, failed)
        return out

    async def process(self, ev):
        mint = ev["mint"]
        sc = ev.get("scanner") or {}
        pair = None
        try:
            pair = fields.best_pair(await self.src.dex_pairs([mint]))   # fresh read of the pool
        except Exception:
            pass
        if pair is None:
            s = sc.get("scan") or {}
            pair = {"liq_usd": s.get("liquidity_usd"), "mcap": s.get("mcap"), "buys_m5": s.get("buys_m5"),
                    "sells_m5": s.get("sells_m5"), "vol_m5": s.get("volume_m5"), "chg_m5": s.get("m5_change_pct"),
                    "buys_h1": s.get("buys_h1"), "sells_h1": s.get("sells_h1"), "vol_h1": s.get("volume_h1"),
                    "chg_h1": s.get("h1_change_pct"), "price_usd": s.get("price"), "created_ms": s.get("created_ms")}
        age = (self.clock() - pair["created_ms"] / 1000) / 60 if pair.get("created_ms") else sc.get("ageMin")
        out = await self.judge(mint, pair, ev.get("auditor") or {}, age)
        name = self.db.coin_name(mint)
        if out["passed"]:
            self.db.update("coins", "mint=?", (mint,), {"status": "sizing"})
            self.decide(mint, "pass", f"score {out['score']} → Executor", out)
            self.bus.publish("executor", {"mint": mint, "scanner": sc, "auditor": ev.get("auditor"), "analyst": out, "pair": pair})
        else:
            self.db.update("coins", "mint=?", (mint,), {"status": "rejected", "status_reason": out["failed"][0]})
            self.db.skip(mint, self.name, out["failed"][0])
            self.decide(mint, "reject", f"{out['label']} · {out['failed'][0]}", out)
            self.bus.activity("rejected", mint, "analyst", f"Rejected {name}", f"{out['label']} · {out['failed'][0]}", "Skipped")
            await self.notifier.push("rejected", f"Rejected {name} · {out['failed'][0]}", mint=mint, buttons=[buy_button(mint)])
        return out

    async def run(self):
        await self.queue_loop("analyst", self.process)
