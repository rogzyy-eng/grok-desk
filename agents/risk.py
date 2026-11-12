"""AGENT 5 · RISK — guards every open position every [poll_s]. Exits at the polled price that fired
it (even if it gapped past the line). Never moves a stop down; never waits for a bounce; can't be
paused while a position is open."""
import fees
import fields
from agents.base import Agent, iso
from notifier import sell_button
from sources import FetchError


def lines(p, c):
    """-> (stop_price, trail_line or None, exit_line, trail_active)."""
    entry, peak = p["entry_price"], p["peak_price"] or p["entry_price"]
    stop = p["stop_price"] if p["stop_price"] is not None else entry * (1 - p["stop_pct"] / 100)
    active = bool(p["trail_active"]) or peak >= entry * (1 + c["trail_start_pct"] / 100)
    trail = peak * (1 - c["trail_pct"] / 100) if active else None
    return stop, trail, max(stop, trail or 0), active


def check_exit(p, price, now, c):
    stop, trail, _, active = lines(p, c)
    if price <= stop:
        return "stop"
    if active and price <= trail:
        return "trail"
    pct = (price / p["entry_price"] - 1) * 100
    if (now - p["opened_at"]) / 3600 >= c["timeout_h"] and pct < c["timeout_min_gain_pct"]:
        return "timeout"
    return None


class Risk(Agent):
    name = "risk"

    def paused(self):
        return False if self.db.open_positions() else super().paused()

    def tighten(self, p, c):
        """A new stop setting applies to open positions only if it is tighter."""
        s = float(c["stop_pct"])
        if s < (p["stop_pct"] or 1e9):
            new = p["entry_price"] * (1 - s / 100)
            if new > (p["stop_price"] or 0):
                self.db.update("positions", "id=?", (p["id"],), {"stop_pct": s, "stop_price": new})
                p = {**p, "stop_pct": s, "stop_price": new}
        return p

    async def tick(self, prices=None):
        c = self.cfg()
        now = self.clock()
        for p in self.db.q("SELECT * FROM positions WHERE status='exiting'"):
            await self.repush(p, c, now)
        pos = [p for p in self.db.q("SELECT * FROM positions WHERE status='open'")]
        if not pos:
            return []
        liqs = {}
        if prices is None:
            prices = {}
            try:
                pairs = await self.src.dex_pairs(list({p["mint"] for p in pos}))
            except FetchError as e:
                if e.status == 429:
                    self.log.warning("429 from DexScreener: backing off %ss, %d polls missed", c["backoff_429_s"], len(pos))
                    await self.sleep(c["backoff_429_s"])
                return []
            for m in {p["mint"] for p in pos}:
                b = fields.best_pair([x for x in pairs or [] if (x.get("baseToken") or {}).get("address", m) == m])
                if b and b["price_usd"]:
                    prices[m], liqs[m] = b["price_usd"], b["liq_usd"]
                    self.db.insert("prices", {"mint": m, "ts": now, "price": b["price_usd"], "volume_m5": b["vol_m5"],
                                              "liquidity_usd": b["liq_usd"]})
        exits = []
        for p in pos:
            p = self.tighten(p, c)
            name = self.db.coin_name(p["mint"])
            price = prices.get(p["mint"]) if isinstance(prices.get(p["mint"]), (int, float)) else None
            if isinstance(prices.get(p["mint"]), dict):
                price, liqs[p["mint"]] = prices[p["mint"]]["price"], prices[p["mint"]].get("liq")
            if price is None:   # never invent a price
                n = (p["no_price_polls"] or 0) + 1
                self.db.update("positions", "id=?", (p["id"],), {"no_price_polls": n})
                self.log.info("%s no price (%d)", p["mint"], n)
                if n == c["no_price_polls"]:
                    await self.notifier.push("noprice", f"{name}: no price for 1 min", mint=p["mint"], buttons=[sell_button(p["mint"])])
                continue
            peak = max(p["peak_price"] or price, price)
            p = {**p, "peak_price": peak, "last_price": price, "no_price_polls": 0}
            stop, trail, exit_line, active = lines(p, c)
            reason = "pulled" if liqs.get(p["mint"]) == 0 else check_exit(p, price, now, c)
            self.db.update("positions", "id=?", (p["id"],), {"peak_price": peak, "last_price": price, "no_price_polls": 0,
                                                             "trail_active": int(active), "exit_line": exit_line})
            self.log.info("%s price=%s peak=%s stop=%s trail=%s action=%s", p["mint"], price, peak, stop, trail, reason or "hold")
            if reason:
                exits.append(await self.exit(p, price, reason, c))
        self.db.snapshot_balance(now)
        return exits

    async def exit(self, p, price, reason, c):
        now = self.clock()
        name = self.db.coin_name(p["mint"])
        disc = float(self.db.get("desk.fees_ref_discount", 0.10))
        gross = (p["qty"] or 0) * price
        xfee = fees.side_fee(max(gross, fees.MIN_TRADE), disc)
        pnl = gross - xfee - p["size_usd"]
        pct = pnl / p["size_usd"] * 100 if p["size_usd"] else 0
        sp = p["stop_pct"]
        text = {"trail": f"Sold {name} · +${pnl:.2f} (+{pct:.1f}%)" if pnl >= 0 else f"Sold {name} · -${-pnl:.2f} ({pct:.1f}%)",
                "stop": f"{name} hit the -{sp:g}% stop · -${abs(pnl):.2f}",
                "timeout": f"Sell {name}, dead money · {pct:+.1f}% in {c['timeout_h']:g}h",
                "pulled": f"{name}: liquidity pulled · sell now"}[reason]
        sub = {"trail": "Risk · trailing stop", "stop": f"Risk · -{sp:g}% stop",
               "timeout": f"Risk · timeout {c['timeout_h']:g}h", "pulled": "Risk · liquidity pulled"}[reason]
        value = f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f} / {pct:+.1f}%"
        if p["mode"] == "PAPER":
            self.db.update("positions", "id=?", (p["id"],), {"status": "closed", "closed_at": now, "exit_price": price,
                           "exit_reason": reason, "pnl_usd": round(pnl, 4), "fees_usd": round((p["entry_fee"] or 0) + xfee, 4)})
            self.db.move_cash(gross - xfee, f"paper sell {name}: {reason}", "PAPER")
            status = "closed"
        else:
            self.db.update("positions", "id=?", (p["id"],), {"status": "exiting", "exit_price": price, "exit_reason": reason,
                                                             "exit_requested_at": now})
            status = "exiting"
        self.bus.activity("stopped" if reason == "stop" else "sold", p["mint"], "risk",
                          f"{'Stopped' if reason == 'stop' else 'Sold'} {name}", sub, value)
        buttons = [sell_button(p["mint"])] + ([{"label": "Sold", "action": "sold", "mint": p["mint"]}] if p["mode"] == "LIVE" else [])
        await self.notifier.push(reason, text, mint=p["mint"], buttons=buttons)
        out = {"agent": "risk", "mint": p["mint"], "name": name, "ts": iso(now), "reason": reason,
               "entryPrice": p["entry_price"], "exitPrice": price, "peakPrice": p["peak_price"], "pnlUsd": round(pnl, 2),
               "pct": round(pct, 2), "feesUsd": round((p["entry_fee"] or 0) + xfee, 2), "stopPct": sp, "mode": p["mode"],
               "status": status, "link": buttons[0]["url"]}
        self.decide(p["mint"], reason, text, out)
        self.log.info("exit %s", out)
        return out

    async def repush(self, p, c, now):
        """LIVE: no [Sold] tap in [sold_wait_min] → push again once, "still open"."""
        if p["repushed"] or now - (p["exit_requested_at"] or now) < c["sold_wait_min"] * 60:
            return
        self.db.update("positions", "id=?", (p["id"],), {"repushed": 1})
        await self.notifier.push("still_open", f"{self.db.coin_name(p['mint'])} still open", mint=p["mint"],
                                 buttons=[sell_button(p["mint"]), {"label": "Sold", "action": "sold", "mint": p["mint"]}])

    async def run(self):
        await self.loop("risk.poll_s", self.tick)
