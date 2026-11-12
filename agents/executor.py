"""AGENT 4 · EXECUTOR — sizes and opens. NEVER places an order on FOMO.
PAPER: simulated fill with fees.py. LIVE: push [Buy on FOMO] + [Bought]; the position waits
[confirm_min] for the tap, else it expires. Size = stack × [stack_pct]%, bumped to (or skipped
under) [min_ticket_usd], capped at [max_liq_pct]% of pool liquidity ($40,000 → $800)."""
import fees
import fields
import links
from agents.base import Agent
from notifier import buy_button


def size_ticket(stack, c, liq=None):
    t = round(stack * c["stack_pct"] / 100.0, 2)
    note = f"{c['stack_pct']:g}% of ${stack:.2f}"
    if t < c["min_ticket_usd"]:
        if c.get("below_min") == "bump" and stack >= c["min_ticket_usd"]:
            t, note = float(c["min_ticket_usd"]), f"{c['stack_pct']:g}% = ${t:.2f} → bumped to ${c['min_ticket_usd']:.2f}"
        else:
            return None, f"${t:.2f} under ${c['min_ticket_usd']:.2f} min ticket"
    if liq:
        cap = round(liq * c["max_liq_pct"] / 100.0, 2)
        if t > cap:
            t, note = cap, note + f" · capped at {c['max_liq_pct']:g}% of liquidity (${cap:.2f})"
    return t, note


def skip_warning(db):
    """PAPER start with below_min=skip and a stack under min_stack: every coin will be skipped."""
    c = db.section("executor")
    stack = stack_now(db)
    if c["below_min"] == "skip" and stack * c["stack_pct"] / 100 < c["min_ticket_usd"]:
        need = round(c["min_ticket_usd"] / (c["stack_pct"] / 100), 2)
        return (f"every coin will be skipped: {c['stack_pct']:g}% of ${stack:.2f} is under the "
                f"${c['min_ticket_usd']:.2f} min ticket (needs ${need:.2f})")
    return None


def stack_now(db):
    return float(db.get("desk.live_stack") or 0) if db.get("desk.mode") == "LIVE" else db.cash()


class Executor(Agent):
    name = "executor"

    async def fresh(self, mint):
        try:
            return fields.best_pair(await self.src.dex_pairs([mint]))
        except Exception:
            return None

    @staticmethod
    def recheck(ref, now_pair, c):
        if not now_pair or not now_pair.get("price_usd"):
            return False, "recheck: no fresh price"
        l0, l1 = ref.get("liq_usd") or 0, now_pair.get("liq_usd") or 0
        if l0 and (l0 - l1) / l0 * 100 > c["recheck_liq_drop_pct"]:
            return False, f"liquidity -{(l0 - l1) / l0 * 100:.0f}% since Scanner"
        p0, p1 = ref.get("price_usd") or 0, now_pair["price_usd"]
        if p0 and abs(p1 - p0) / p0 * 100 > c["recheck_price_move_pct"]:
            return False, f"price {(p1 - p0) / p0 * 100:+.0f}% since Scanner"
        return True, "ok"

    async def process(self, ev):
        mint = ev["mint"]
        c = self.cfg()
        mode = self.db.get("desk.mode", "PAPER")
        now = self.clock()
        name = self.db.coin_name(mint)
        if len(self.db.open_positions()) >= c["max_open"]:
            return self._skip(mint, f"{c['max_open']} positions already open")
        if self.db.one("SELECT 1 FROM positions WHERE mint=? AND status IN ('open','pending','exiting')", (mint,)):
            return self._skip(mint, "already holding")
        ref = ev.get("pair") or {}
        p1 = await self.fresh(mint)
        ok, why = self.recheck(ref, p1, c)
        if not ok:
            return self._skip(mint, why)
        size, note = size_ticket(stack_now(self.db), c, p1.get("liq_usd"))
        if size is None:
            return self._skip(mint, note)
        price = p1["price_usd"]
        fee = fees.side_fee(size, float(self.db.get("desk.fees_ref_discount", 0.10)))
        stop_pct = float(self.db.get("risk.stop_pct"))
        link = links.coin_url(mint, "solana")
        sid = self.db.insert("signals", {"mint": mint, "ts": now, "price": price, "size_usd": size, "stack_pct": c["stack_pct"],
                                         "link": link, "mode": mode, "status": "filled" if mode == "PAPER" else "pending",
                                         "side": "buy", "reason": note})
        pos = {"mint": mint, "opened_at": now, "entry_price": price, "size_usd": size, "peak_price": price,
               "stop_pct": stop_pct, "stop_price": price * (1 - stop_pct / 100), "trail_active": 0, "mode": mode,
               "entry_fee": fee, "fees_usd": fee, "last_price": price, "signal_id": sid}
        if mode == "PAPER":
            pid = self.db.insert("positions", {**pos, "status": "open", "qty": (size - fee) / price})
            self.db.move_cash(-size, f"paper buy {name} #{pid}", "PAPER")
            self.bus.activity("bought", mint, "executor", f"Bought {name}", f"Executor · {note}", f"${size:.2f}")
            await self.notifier.push("bought", f"Bought {name} · ${size:.2f}", mint=mint, buttons=[buy_button(mint)])
        else:
            pid = self.db.insert("positions", {**pos, "status": "pending", "qty": 0})
            await self.notifier.push("buy", f"Buy {name} · ${size:.2f}", mint=mint,
                                     buttons=[buy_button(mint), {"label": "Bought", "action": "bought", "mint": mint}])
        self.decide(mint, "open" if mode == "PAPER" else "signal", f"{mode} ${size:.2f} @ {price:.8g} · {note}")
        self.log.info("%s %s size=%.2f price=%s fee=%.4f %s", mode, mint, size, price, fee, note)
        return pid

    async def expire_pending(self):
        """LIVE: no [Bought] tap within [confirm_min] → the signal expires (nothing was bought)."""
        lim = float(self.db.get("executor.confirm_min", 5)) * 60
        for p in self.db.q("SELECT * FROM positions WHERE status='pending' AND opened_at<?", (self.clock() - lim,)):
            self.db.update("positions", "id=?", (p["id"],), {"status": "expired", "closed_at": self.clock()})
            self.db.update("signals", "id=?", (p["signal_id"],), {"status": "expired"})
            self.decide(p["mint"], "expired", "no [Bought] tap in time")

    def _skip(self, mint, reason):
        self.db.update("coins", "mint=?", (mint,), {"status": "skipped", "status_reason": reason})
        self.db.skip(mint, self.name, reason)
        self.decide(mint, "skip", reason)
        self.log.info("skip %s %s", mint, reason)
        return None

    async def run(self):
        import asyncio
        await asyncio.gather(self.queue_loop("executor", self.process), self.loop("executor.tick_s", self.expire_pending))
