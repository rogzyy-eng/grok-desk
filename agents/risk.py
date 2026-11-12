"""AGENT 5 · RISK — guards every open position. Price source (risk.price_source "auto"): the pool's on-chain
reserves over a Solana websocket (onchain.Watcher, evaluated on every account update), checked once against
DexScreener (> risk.ws_max_diff_pct apart = poll only); otherwise / "poll" / socket down: DexScreener every
[poll_s]. Exits at the price that fired it (even if it gapped past the line). rug_early sells when the pool's SOL
drains fast or the creator dumps; pulled stays as the backstop. First trail hit sells half; the runner trails
wider. Never moves a stop down; never waits for a bounce; can't be paused while a position is open."""
import asyncio

import fees
import fields
from onchain import fast_drain
from agents.base import Agent, iso
from notifier import sell_button
from sources import FetchError


SCALED = ("stop_pct", "trail_start_pct", "trail_pct", "timeout_h", "runner_trail_pct")


def pos_cfg(p, c):
    """Risk settings for one position. Positions opened since 2026-11-12 carry a snapshot of stop / trail
    start / trail / timeout / runner trail taken at open (volatile × factor already applied), so later
    setting changes never touch them. Older positions (no snapshot) fall back to the live settings,
    volatile ones × [volatile_factor]."""
    if c.get("_pos"):
        return c
    if p.get("trail_pct") is not None:
        snap = {k: float(p[k]) for k in SCALED if p.get(k) is not None}
        return {**c, **snap, "_pos": True}
    if not p.get("volatile"):
        return {**c, "_pos": True}
    f = float(c.get("volatile_factor", 1))
    return {**c, **{k: round(float(c[k]) * f, 4) for k in SCALED if k in c}, "_pos": True}


def lines(p, c):
    """-> (stop_price, trail_line or None, exit_line, trail_active). After the first trail hit sold half
    (runner), the rest trails [runner_trail_pct] off the top."""
    c = pos_cfg(p, c)
    entry, peak = p["entry_price"], p["peak_price"] or p["entry_price"]
    stop = p["stop_price"] if p["stop_price"] is not None else entry * (1 - c["stop_pct"] / 100)
    active = bool(p["trail_active"]) or peak >= entry * (1 + c["trail_start_pct"] / 100)
    tp = c.get("runner_trail_pct", 30) if p.get("runner") else c["trail_pct"]
    trail = peak * (1 - tp / 100) if active else None
    return stop, trail, max(stop, trail or 0), active


def check_exit(p, price, now, c):
    c = pos_cfg(p, c)
    stop, trail, _, active = lines(p, c)
    if price <= stop:
        return "stop"
    if active and price <= trail:
        return "trail"
    pct = (price / p["entry_price"] - 1) * 100
    if (now - p["opened_at"]) / 3600 >= c["timeout_h"] and pct < c["timeout_min_gain_pct"]:
        return "timeout"
    return None


def pulled(p, price, liq, c):
    """Liquidity pulled: pool at 0, pool down > [pulled_liq_drop_pct] from entry, or price down
    > [pulled_price_drop_pct] between two polls. -> reason text or None."""
    if liq is not None:
        if liq <= 0:
            return "liquidity 0"
        e = p.get("entry_liq_usd")
        if e and liq < e * (1 - float(c.get("pulled_liq_drop_pct", 50)) / 100):
            return f"liquidity {(liq / e - 1) * 100:.0f}% since entry"
    prev = p.get("last_price")
    if prev and price < prev * (1 - float(c.get("pulled_price_drop_pct", 50)) / 100):
        return f"price {(price / prev - 1) * 100:.0f}% in one poll"
    return None


def rug_early(st, now, c):
    """Exit before the crash. -> Turkish reason or None. Uses the on-chain pool state only:
    quote-side reserve (SOL in the pool) down >= [rug_liq_drop_fast_pct] within [rug_window_s], or the creator
    wallet (held >= [rug_dev_min_supply_pct] of supply when watching started) sold >= [rug_dev_sell_pct] of it."""
    if st is None:
        return None
    drop = fast_drain(st.hist, now, float(c.get("rug_window_s", 20)), float(c.get("rug_liq_drop_fast_pct", 25)))
    if drop is not None:
        return f"havuzdaki SOL {drop:.0f}% / {float(c.get('rug_window_s', 20)):g}sn"
    if st.dev_start and st.dev_now is not None and (st.dev_supply_pct or 0) >= float(c.get("rug_dev_min_supply_pct", 1)):
        sold = (1 - st.dev_now / st.dev_start) * 100
        if sold >= float(c.get("rug_dev_sell_pct", 50)):
            return f"geliştirici cüzdanı payının %{sold:.0f}'ini sattı"
    return None


def src_tr(source):
    return {"zincir": "zincir", "DexScreener": "DexScreener"}.get(source, source)


class Risk(Agent):
    name = "risk"
    watcher = None
    _lock = None

    @property
    def lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def paused(self):
        return False if self.db.open_positions() else super().paused()

    # ---- on-chain price (onchain.Watcher) ----
    def sync_watch(self, pos, c):
        """Subscribe each open position's pool (coins.pool = DexScreener pairAddress); drop closed ones."""
        w = self.watcher
        if w is None:
            return
        want = {p["mint"] for p in pos} if c.get("price_source", "auto") == "auto" else set()
        for m in list(w.pools):
            if m not in want:
                w.unwatch(m)
        now = self.clock()
        self._retry = getattr(self, "_retry", {})
        for m in want:
            if m in w.pools or m in w.unsupported or self._retry.get(m, 0) > now:
                continue
            coin = self.db.one("SELECT pool, creator FROM coins WHERE mint=?", (m,)) or {}
            if not coin.get("pool"):
                continue
            self._retry[m] = now + 30
            asyncio.ensure_future(self._watch(m, coin["pool"], coin.get("creator")))

    async def _watch(self, mint, pool, creator):
        try:
            await self.watcher.watch(mint, pool, creator)
        except Exception as e:
            self.log.info("%s ws watch failed (%s); polling", mint, type(e).__name__)

    def ws_price(self, p, c):
        """-> (price_usd, liq_usd, state) when a trusted on-chain price exists, else None (DexScreener)."""
        w = self.watcher
        if w is None or not w.connected or c.get("price_source", "auto") != "auto":
            return None
        st = w.state(p["mint"])
        if st is None or st.price_native is None or not st.quote_usd:
            return None
        if st.trusted is None:                 # first on-chain price: sanity check against DexScreener
            if not st.dex_native:
                return None
            diff = (st.price_native / st.dex_native - 1) * 100
            st.diff_pct = round(diff, 2)
            st.trusted = abs(diff) <= float(c.get("ws_max_diff_pct", 20))
            name = self.db.coin_name(p["mint"])
            txt = (f"{name}: zincir fiyatı DexScreener'a göre {diff:+.1f}% ({st.kind}) · "
                   + ("anlık zincir fiyatı kullanılıyor" if st.trusted else "güvenilmedi, DexScreener ile devam"))
            self.decide(p["mint"], "ws_check", txt, {"kind": st.kind, "wsNative": st.price_native,
                                                    "dexNative": st.dex_native, "diffPct": st.diff_pct, "trusted": st.trusted})
            (self.log.info if st.trusted else self.log.warning)("%s ws check %s diff=%.2f%% trusted=%s", p["mint"], st.kind, diff, st.trusted)
        if not st.trusted:
            return None
        q = st.quote_reserve or 0
        return st.price_native * st.quote_usd, 2 * q * st.quote_usd, st

    async def on_ws(self, mint, st, role):
        """Every on-chain pool update: evaluate stop / trail / rug_early for that coin at once."""
        try:
            async with self.lock:
                c = self.cfg()
                now = self.clock()
                for p in self.db.q("SELECT * FROM positions WHERE status='open' AND mint=?", (mint,)):
                    got = self.ws_price(p, c)
                    if got:
                        await self.assess(p, got[0], got[1], c, now, "zincir", rug_early(got[2], now, c))
        except Exception:
            self.log.exception("ws update failed")

    async def tick(self, prices=None):
        c = self.cfg()
        now = self.clock()
        for p in self.db.q("SELECT * FROM positions WHERE status='exiting'"):
            await self.repush(p, c, now)
        pos = [p for p in self.db.q("SELECT * FROM positions WHERE status='open'")]
        polled = prices is None
        if polled:
            self.sync_watch(pos, c)
        if not pos:
            return []
        liqs = {}
        dex_ok, hit_429 = True, False
        if polled:
            prices = {}
            pairs = []
            try:
                pairs = await self.src.dex_pairs(list({p["mint"] for p in pos}))
            except FetchError as e:
                dex_ok, hit_429 = False, e.status == 429
            for m in {p["mint"] for p in pos}:
                mine = [x for x in pairs or [] if (x.get("baseToken") or {}).get("address", m) == m]
                b = fields.best_pair(mine)
                if b and b["price_usd"]:
                    prices[m], liqs[m] = b["price_usd"], b["liq_usd"]
                    self.db.insert("prices", {"mint": m, "ts": now, "price": b["price_usd"], "volume_m5": b["vol_m5"],
                                              "liquidity_usd": b["liq_usd"]})
                if self.watcher is not None and self.watcher.state(m):
                    st = self.watcher.state(m)
                    same = [fields.pair(x) for x in mine if x.get("pairAddress") == st.pool] or ([b] if b else [])
                    if same and same[0].get("price_native"):
                        self.watcher.note_dex(m, same[0]["price_usd"], same[0]["price_native"])
        exits = []
        async with self.lock:
            for p in self.db.q("SELECT * FROM positions WHERE status='open'"):
                got = self.ws_price(p, c) if polled else None
                if got:
                    exits += await self.assess(p, got[0], got[1], c, now, "zincir", rug_early(got[2], now, c))
                    continue
                if not dex_ok:
                    continue
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
                exits += await self.assess(p, price, liqs.get(p["mint"]), c, now, "DexScreener" if polled else None)
        self.db.snapshot_balance(now)
        if hit_429:
            await self.backoff_429(len(pos))
        return exits

    async def assess(self, p, price, liq, c, now, source="DexScreener", rug=None):
        """One price for one open position -> [] or [exit/partial dict]. rug_early > pulled > stop/trail/timeout."""
        why_pulled = pulled(p, price, liq, c)          # reads the previous update's last_price
        peak = max(p["peak_price"] or price, price)
        p = {**p, "peak_price": peak, "last_price": price, "no_price_polls": 0,
             "last_liq_usd": liq if liq is not None else p.get("last_liq_usd")}
        stop, trail, exit_line, active = lines(p, c)
        reason = "rug_early" if rug else ("pulled" if why_pulled else check_exit(p, price, now, c))
        self.db.update("positions", "id=?", (p["id"],), {"peak_price": peak, "last_price": price, "no_price_polls": 0,
                                                         "trail_active": int(active), "exit_line": exit_line,
                                                         "last_liq_usd": p["last_liq_usd"], "price_source": source or "DexScreener"})
        self.log.info("%s price=%s (%s) peak=%s stop=%s trail=%s action=%s", p["mint"], price, source, peak, stop, trail, reason or "hold")
        if reason == "trail" and not p.get("runner"):
            half = await self.sell_half(p, price, liq, c, source)
            if half:
                return [half]
        if reason:
            return [await self.exit(p, price, reason, c, liq=liq, detail=rug or why_pulled, source=source)]
        return []

    def exit_liq(self, p, liq):
        return liq if liq is not None else (p.get("last_liq_usd") or p.get("entry_liq_usd"))

    def sell_value(self, p, qty, price, liq):
        """-> (gross USD, impact %). PAPER pays constant-product price impact; LIVE is the polled price."""
        if p["mode"] == "PAPER":
            return fees.sell_impact(qty, price, self.exit_liq(p, liq))
        return (qty or 0) * price, 0.0

    async def sell_half(self, p, price, liq, c, source=None):
        """First trail hit: sell half (FOMO fee on this side), keep the rest as a runner trailing
        [runner_trail_pct] off the top. A half under FOMO's $5 minimum -> None (sell it all)."""
        half = (p["qty"] or 0) / 2
        if half * price < fees.MIN_TRADE:
            return None
        now = self.clock()
        name = self.db.coin_name(p["mint"])
        disc = float(self.db.get("desk.fees_ref_discount", 0.10))
        gross, imp = self.sell_value(p, half, price, liq)
        pfee = fees.side_fee(max(gross, fees.MIN_TRADE), disc) if gross > 0 else 0.0
        net = gross - pfee
        pnl = net - p["size_usd"] / 2           # realised on the half
        rt = pos_cfg(p, c).get("runner_trail_pct", 30)
        self.db.update("positions", "id=?", (p["id"],), {
            "runner": 1, "qty": p["qty"] - half, "partial_at": now, "partial_qty": half, "partial_price": price,
            "partial_usd": round(net, 6), "partial_fee": round(pfee, 6),
            "fees_usd": round((p["entry_fee"] or 0) + pfee, 6)})
        if p["mode"] == "PAPER":
            self.db.move_cash(net, f"paper sell half {name}: trail", "PAPER")
        imp_txt = f" · impact {imp:.2f}%" if imp else ""
        value = f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}"
        self.bus.activity("sold", p["mint"], "risk", f"Sold half {name}",
                          f"Risk · trail hit, runner trails {rt:g}%{imp_txt}" + (f" · fiyat: {src_tr(source)}" if source else ""), value)
        text = (f"Sold half {name}" if p["mode"] == "PAPER" else f"Sell half {name} on FOMO") + f" · {value} · rest trails {rt:g}%"
        btn = sell_button(p["mint"])
        await self.notifier.push("trail", text, mint=p["mint"], buttons=[btn])
        out = {"agent": "risk", "mint": p["mint"], "name": name, "ts": iso(now), "reason": "partial",
               "entryPrice": p["entry_price"], "exitPrice": price, "qty": half, "grossUsd": round(gross, 4),
               "feeUsd": round(pfee, 4), "pnlUsd": round(pnl, 2), "impactPct": imp, "runnerTrailPct": rt,
               "mode": p["mode"], "status": "partial", "link": btn["url"]}
        self.decide(p["mint"], "partial", text, out)
        self.log.info("partial %s", out)
        return out

    async def exit(self, p, price, reason, c, liq=None, detail=None, source=None):
        now = self.clock()
        name = self.db.coin_name(p["mint"])
        disc = float(self.db.get("desk.fees_ref_discount", 0.10))
        gross, imp = self.sell_value(p, p["qty"], price, liq)
        xfee = fees.side_fee(max(gross, fees.MIN_TRADE), disc) if gross > 0 else 0.0
        part = p.get("partial_usd") or 0
        pfee = p.get("partial_fee") or 0
        pnl = part + gross - xfee - p["size_usd"]
        pct = pnl / p["size_usd"] * 100 if p["size_usd"] else 0
        c = pos_cfg(p, c)
        sp = p["stop_pct"]
        vol = " (volatil)" if p.get("volatile") else ""
        run = " (runner)" if p.get("runner") else ""
        imp_txt = f" · impact {imp:.2f}%" if imp else ""
        text = {"trail": (f"Sold {name}{run} · +${pnl:.2f} (+{pct:.1f}%)" if pnl >= 0 else f"Sold {name}{run} · -${-pnl:.2f} ({pct:.1f}%)"),
                "stop": f"{name} hit the -{sp:g}% stop{vol} · -${abs(pnl):.2f}",
                "timeout": f"Sell {name}, dead money · {pct:+.1f}% in {c['timeout_h']:g}h",
                "pulled": f"{name}: liquidity pulled · sell now",
                "rug_early": f"{name}: rug erken uyarısı · hemen sat · {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}"}[reason]
        sub = {"trail": f"Risk · trailing stop{run}", "stop": f"Risk · -{sp:g}% stop{vol}",
               "timeout": f"Risk · timeout {c['timeout_h']:g}h",
               "pulled": "Risk · liquidity pulled" + (f" ({detail})" if detail else ""),
               "rug_early": "Risk · rug erken uyarı" + (f" ({detail})" if detail else "")}[reason] + imp_txt
        if source:
            text += f" · fiyat: {src_tr(source)}"
            sub += f" · fiyat: {src_tr(source)}"
        value = f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f} / {pct:+.1f}%"
        fees_total = round((p["entry_fee"] or 0) + pfee + xfee, 4)
        if p["mode"] == "PAPER":
            self.db.update("positions", "id=?", (p["id"],), {"status": "closed", "closed_at": now, "exit_price": price,
                           "exit_reason": reason, "pnl_usd": round(pnl, 4), "fees_usd": fees_total})
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
               "pct": round(pct, 2), "feesUsd": round(fees_total, 2), "stopPct": sp, "volatile": bool(p.get("volatile")), "runner": bool(p.get("runner")),
               "impactPct": imp, "mode": p["mode"], "priceSource": source, "detail": detail,
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
        import config
        from onchain import Watcher
        if self.watcher is None and hasattr(self.src, "rpc"):
            self.watcher = Watcher(config.ws_url(self.db), self.src.rpc, self.on_ws, clock=self.clock)
        if self.watcher is None:
            return await self.loop("risk.poll_s", self.tick)
        await asyncio.gather(self.loop("risk.poll_s", self.tick), self.watcher.run())
