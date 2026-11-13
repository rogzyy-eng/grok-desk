"""AGENT 6 · COPY — watches configured Solana wallets (copy.wallets) over the desk RPC/ws.
When one buys a token with SOL/USDC (notional >= copy.min_buy_usd), the mint goes through the
Auditor, then the Executor opens a PAPER position (source=copy). Never calls FOMO, never places
an order. LIVE stays locked: copy does not open positions outside PAPER. Wallets are read from
the setting, not hardcoded here."""
import json
import re

import fields
from agents.base import Agent
from agents.executor import open_count, reentry_skip_reason
from notifier import buy_button
from onchain import WalletWatcher, wallet_flows
from sources import FetchError

ADDR_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def parse_wallets(value):
    """Normalize copy.wallets into [{handle, address}, ...]. Accepts a list of dicts,
    a list of addresses, JSON text, or lines like '@leo ADDRESS'."""
    if not value:
        return []
    if isinstance(value, str):
        raw = value.strip()
        try:
            value = json.loads(raw)
        except ValueError:
            rows = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.replace(",", " ").split()
                handle, addr = "", None
                for p in parts:
                    p = p.lstrip("@")
                    if ADDR_RE.fullmatch(p):
                        addr = p
                    elif p:
                        handle = p
                if addr:
                    rows.append({"handle": handle or addr[:4], "address": addr})
            return _dedupe(rows)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    rows = []
    for item in value:
        if isinstance(item, str):
            item = item.strip()
            if ADDR_RE.fullmatch(item):
                rows.append({"handle": item[:4], "address": item})
            continue
        if not isinstance(item, dict):
            continue
        addr = (item.get("address") or item.get("wallet") or "").strip()
        handle = (item.get("handle") or item.get("name") or "").strip().lstrip("@")
        if ADDR_RE.fullmatch(addr):
            rows.append({"handle": handle or addr[:4], "address": addr})
    return _dedupe(rows)


def _dedupe(rows):
    seen, out = set(), []
    for r in rows:
        a = r["address"]
        if a in seen:
            continue
        seen.add(a)
        out.append(r)
    return out


def fmt_quote(quote, amount):
    amount = float(amount or 0)
    if (quote or "SOL").upper() == "USDC":
        s = f"{amount:.2f}".rstrip("0").rstrip(".")
        return f"{s} USDC"
    if amount >= 0.1:
        return f"{amount:.1f} SOL"
    return f"{amount:.3f} SOL"


def handle_of(wallets, address):
    for w in wallets:
        if w["address"] == address:
            return w["handle"]
    return (address or "")[:4]


class Copy(Agent):
    name = "copy"

    def __init__(self, *a, auditor=None, executor=None, risk=None, **kw):
        super().__init__(*a, **kw)
        self.auditor, self.executor, self.risk = auditor, executor, risk
        self.watcher = None
        self._busy = set()          # mints currently being processed
        self.sol_usd = None
        self.beat("ok", "idle")

    def wallets(self):
        return parse_wallets(self.db.get("copy.wallets") or [])

    def by_addr(self):
        return {w["address"]: w["handle"] for w in self.wallets()}

    def _busy_add(self, mint):
        if mint in self._busy:
            return False
        self._busy.add(mint)
        return True

    def quote_usd(self, quote, amount, token_usd=None, token_amount=None):
        if (quote or "").upper() == "USDC":
            return float(amount or 0)
        if self.sol_usd:
            return float(amount or 0) * float(self.sol_usd)
        if token_usd and token_amount:
            return float(token_amount) * float(token_usd)
        return None

    async def refresh_sol_usd(self):
        try:
            from onchain import WSOL
            pair = fields.best_pair(await self.src.dex_pairs([WSOL]))
            if pair and pair.get("price_usd"):
                self.sol_usd = float(pair["price_usd"])
        except FetchError as e:
            if e.status == 429:
                await self.backoff_429(where="DexScreener")
        except Exception:
            pass

    async def on_tx(self, wallet, sig, tx):
        if self.paused() or not self.db.get("copy.enabled", True):
            return
        flows = wallet_flows(tx, wallet)
        if not flows:
            return
        handle = self.by_addr().get(wallet) or handle_of(self.wallets(), wallet)
        now = self.clock()
        # sells first so a sell+buy tx can close then (not) reopen
        for s in flows.get("sells") or []:
            await self.on_sell(handle, wallet, sig, s, flows.get("block_time"), now)
        for b in flows.get("buys") or []:
            await self.on_buy(handle, wallet, sig, b, flows.get("block_time"), now)

    async def on_buy(self, handle, wallet, sig, buy, block_time, now):
        mint = buy["mint"]
        tag = f"@{handle}" if handle and not handle.startswith("@") else (handle or "cüzdan")
        qtxt = fmt_quote(buy.get("quote"), buy.get("quote_amount"))
        age_s = None if not block_time else now - float(block_time)
        max_age = float(self.db.get("copy.max_age_s", 60) or 0)
        if age_s is not None and max_age > 0 and age_s > max_age:
            self.decide(mint, "skip", f"kopya: {tag} {qtxt} aldı → sinyal {age_s:.0f}s eski, atladım")
            return
        if not self._busy_add(mint):
            return
        try:
            await self._buy(handle, tag, wallet, sig, buy, qtxt, now)
        finally:
            self._busy.discard(mint)

    async def _buy(self, handle, tag, wallet, sig, buy, qtxt, now):
        mint = buy["mint"]
        mode = self.db.get("desk.mode", "PAPER")
        if mode != "PAPER":
            self.decide(mint, "skip", f"kopya: {tag} {qtxt} aldı → sadece PAPER'da açılır")
            return
        if self.db.one("SELECT 1 FROM positions WHERE copy_sig=?", (sig,)):
            return
        cap = int(self.db.get("copy.max_open", 3) or 0)
        if open_count(self.db, "copy") >= cap:
            self.decide(mint, "skip", f"kopya: {tag} {qtxt} aldı → kopya limiti doldu ({cap})")
            return
        blocked = reentry_skip_reason(self.db, mint, now, self.db.get("executor.reentry_cooldown_min", 60))
        if blocked:
            self.decide(mint, "skip", f"kopya: {tag} {qtxt} aldı → {blocked}")
            return
        pair = await self.fresh_pair(mint)
        if not pair or not pair.get("price_usd"):
            self.decide(mint, "skip", f"kopya: {tag} {qtxt} aldı → fiyat yok")
            return
        notional = self.quote_usd(buy.get("quote"), buy.get("quote_amount"),
                                  pair.get("price_usd"), buy.get("token_amount"))
        if notional is None:
            await self.refresh_sol_usd()
            notional = self.quote_usd(buy.get("quote"), buy.get("quote_amount"),
                                      pair.get("price_usd"), buy.get("token_amount"))
        min_buy = float(self.db.get("copy.min_buy_usd", 100) or 0)
        if notional is None or notional < min_buy:
            shown = "fiyat yok" if notional is None else f"${notional:.0f} < ${min_buy:.0f}"
            self.decide(mint, "skip", f"kopya: {tag} {qtxt} aldı → min alış {shown}")
            return
        self.ensure_coin(mint, pair, now)
        scan = {"pool": pair.get("pair_address"), "created_ms": pair.get("created_ms"),
                "top10_pct": None, "liq_usd": pair.get("liq_usd"), "price_usd": pair.get("price_usd")}
        out = await self._audit(mint, scan, now)
        if not out or not out.get("passed"):
            reason = (out or {}).get("reason") or "denetim başarısız"
            text = f"kopya: {tag} {qtxt} aldı → denetim reddetti"
            self.decide(mint, "reject", f"{text} ({reason})", out)
            self.bus.activity("rejected", mint, "copy", text, f"Copy · {reason}", tag)
            if self.db.get("desk.push_rejects"):
                await self.notifier.push("rejected", f"{text} · {reason}", mint=mint, buttons=[buy_button(mint)])
            return
        if self.executor is None:
            return
        pid = await self.executor.open_copy({
            "mint": mint, "pair": pair, "handle": handle, "wallet": wallet, "sig": sig,
            "start_qty": buy.get("token_post"), "note": f"kopya {tag} {qtxt}",
        })
        if not pid:
            # executor logged the skip (gap, ticket, reentry…)
            return
        text = f"kopya: {tag} {qtxt} aldı → denetimden geçti, PAPER aldım"
        self.decide(mint, "open", text, {"handle": handle, "wallet": wallet, "sig": sig, "notional": notional})
        self.bus.activity("bought", mint, "copy", text, "Copy · PAPER", f"${notional:.0f}")
        await self.notifier.push("bought", text, mint=mint, buttons=[buy_button(mint)])

    async def _audit(self, mint, scan, now):
        if not self.db.get("auditor.enabled", True):
            return {"passed": True, "reason": None, "layers": []}
        if self.auditor is None:
            return {"passed": False, "reason": "auditor missing"}
        if self.auditor.rejected_tonight(mint):
            row = self.db.one("SELECT value_text FROM audits WHERE mint=? AND pass=0 ORDER BY id DESC", (mint,))
            return {"passed": False, "reason": (row or {}).get("value_text") or "already rejected tonight"}
        return await self.auditor.audit(mint, scan, now=now)

    def ensure_coin(self, mint, pair, now):
        row = self.db.one("SELECT mint, symbol, pool FROM coins WHERE mint=?", (mint,))
        fields_row = {"name": pair.get("name"), "symbol": pair.get("symbol"), "pool": pair.get("pair_address"),
                      "chain": "solana", "updated_at": now}
        if row:
            patch = {k: v for k, v in fields_row.items() if v and not row.get(k)}
            if patch:
                self.db.update("coins", "mint=?", (mint,), patch)
            return
        self.db.insert("coins", {"mint": mint, "status": "auditing", "migrated_at": now, **fields_row})

    async def fresh_pair(self, mint):
        try:
            return fields.best_pair(await self.src.dex_pairs([mint]))
        except FetchError as e:
            if e.status == 429:
                await self.backoff_429(where="DexScreener")
            return None
        except Exception:
            return None

    async def on_sell(self, handle, wallet, sig, sell, block_time, now):
        mint = sell["mint"]
        frac = float(self.db.get("copy.follow_sell_pct", 50) or 0)
        if frac <= 0 or self.risk is None:
            return
        pos = self.db.one("SELECT * FROM positions WHERE mint=? AND source='copy' AND copy_wallet=? "
                          "AND status IN ('open','pending') ORDER BY id DESC", (mint, wallet))
        if not pos:
            return
        start = pos.get("copy_start_qty")
        if not start:
            return
        post = sell.get("token_post")
        if post is None:
            return
        sold_pct = max(0.0, (float(start) - float(post)) / float(start) * 100)
        if sold_pct + 1e-9 < frac:
            self.decide(mint, "hold", f"kopya: @{handle} %{sold_pct:.0f} sattı (eşik %{frac:g})")
            return
        tag = f"@{handle}" if handle and not handle.startswith("@") else (handle or "cüzdan")
        text = f"kopya: {tag} %{sold_pct:.0f} sattı → çıktım"
        price = pos.get("last_price") or pos.get("entry_price")
        if not price:
            return
        c = self.db.section("risk")
        await self.risk.exit(pos, price, "copy_sell", c,
                             liq=pos.get("last_liq_usd") or pos.get("entry_liq_usd"),
                             detail=text, source=pos.get("price_source") or "DexScreener")
        self.decide(mint, "copy_sell", text, {"sold_pct": sold_pct, "sig": sig})

    async def run(self):
        import asyncio
        import config
        self.beat("ok", "idle")
        rpc = getattr(self.src, "rpc", None)
        if rpc is None:
            self.log.info("no RPC client; copy watcher idle")
            await self.loop("risk.poll_s", self._idle)
            return
        async def backoff():
            await self.backoff_429(where="RPC")
        self.watcher = WalletWatcher(config.ws_url(self.db), rpc, on_tx=self.on_tx,
                                     clock=self.clock, sleep=self.sleep, poll_s=15)
        self.watcher.set_wallets([w["address"] for w in self.wallets()])
        await asyncio.gather(self.watcher.run(backoff_429=backoff), self.loop("risk.poll_s", self._refresh))

    async def _idle(self):
        return None

    async def _refresh(self):
        if self.watcher is not None:
            self.watcher.set_wallets([w["address"] for w in self.wallets()] if self.db.get("copy.enabled", True) else [])
        if self.sol_usd is None:
            await self.refresh_sol_usd()
