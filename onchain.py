"""onchain.py — real-time pool prices for open positions from a Solana RPC websocket (accountSubscribe).

Supported pools (owner program of the DexScreener pairAddress, layouts checked live 2026-11-12):
  pumpfun      6EF8rr… bonding curve account: virtual token / SOL reserves (u64 @8 / @16), real SOL @32
  pumpswap     pAMMBay… pool: base_mint @43, quote_mint @75, base vault @139, quote vault @171, coin_creator @211
  raydium_v4   675kPX… AMM v4: base vault @336, quote vault @368, base mint @400, quote mint @432
  raydium_cpmm CPMMoo… CPMM: vault0 @72, vault1 @104, mint0 @168, mint1 @200
For vault pools the two vault token accounts are subscribed (jsonParsed → tokenAmount) and
price = quote balance / coin balance (in quote units, normally SOL). Anything else (CLMM, Meteora, a decode
that does not contain the coin's mint) returns None and Risk keeps polling DexScreener.
USD = native price × (DexScreener priceUsd / priceNative), refreshed on every DexScreener poll.
The websocket URL can carry an API key: it is never logged."""
import asyncio
import base64
import json
import logging
import struct
import time
from collections import deque

log = logging.getLogger("risk.onchain")

PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_SWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAY_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAY_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
KINDS = {PUMP_FUN: "pumpfun", PUMP_SWAP: "pumpswap", RAY_V4: "raydium_v4", RAY_CPMM: "raydium_cpmm"}
# kind -> (mint_a, mint_b, vault_a, vault_b, min length)
VAULT_LAYOUTS = {"pumpswap": (43, 75, 139, 171, 211), "raydium_v4": (400, 432, 336, 368, 752),
                 "raydium_cpmm": (168, 200, 72, 104, 232)}
HIST_KEEP_S = 130

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def _key(data, off):
    return b58encode(data[off:off + 32])


def decode_pool(owner, data: bytes, mint, address=None):
    """-> {kind, coin_vault, quote_vault, quote_mint, creator} | {kind: pumpfun, curve} | None."""
    kind = KINDS.get(owner)
    if not kind or not data:
        return None
    if kind == "pumpfun":
        cur = decode_curve(data)
        if not cur or cur["complete"] or cur["coin"] <= 0:
            return None
        return {"kind": kind, "curve": address, "creator": _key(data, 49) if len(data) >= 81 else None}
    ma, mb, va, vb, n = VAULT_LAYOUTS[kind]
    if len(data) < n:
        return None
    a, b = _key(data, ma), _key(data, mb)
    if a == mint:
        coin_vault, quote_vault, quote_mint = _key(data, va), _key(data, vb), b
    elif b == mint:
        coin_vault, quote_vault, quote_mint = _key(data, vb), _key(data, va), a
    else:
        return None                      # the layout did not decode to this coin: do not trust it
    creator = _key(data, 211) if kind == "pumpswap" and len(data) >= 243 else None
    if creator == "11111111111111111111111111111111":
        creator = None
    return {"kind": kind, "coin_vault": coin_vault, "quote_vault": quote_vault, "quote_mint": quote_mint, "creator": creator}


def decode_curve(data: bytes):
    """pump.fun bonding curve. Token decimals 6, SOL 9."""
    if not data or len(data) < 49:
        return None
    vtok, vsol, rtok, rsol, _supply = struct.unpack_from("<QQQQQ", data, 8)
    return {"coin": vtok / 1e6, "quote": vsol / 1e9, "real_quote": rsol / 1e9, "complete": bool(data[48])}


def token_ui(value):
    """jsonParsed token account value -> ui amount (float) or None."""
    try:
        ta = value["data"]["parsed"]["info"]["tokenAmount"]
        return float(ta.get("uiAmountString") or ta["uiAmount"] or 0)
    except (KeyError, TypeError, ValueError):
        return None


def b64data(value):
    try:
        d = value["data"]
        return base64.b64decode(d[0] if isinstance(d, list) else d)
    except (KeyError, TypeError, ValueError):
        return None


def fast_drain(hist, now, window_s, drop_pct):
    """Quote-side reserve down >= drop_pct within window_s -> drop % (negative) or None. The baseline includes
    the last value seen before the window, so one big pull after a quiet minute still counts."""
    if not hist:
        return None
    cur = hist[-1][1]
    start = now - window_s
    base = [q for t, q in hist if t >= start]
    before = [q for t, q in hist if t < start]
    if before:
        base.append(before[-1])
    peak = max(base) if base else 0
    if peak > 0 and cur <= peak * (1 - drop_pct / 100):
        return (cur / peak - 1) * 100
    return None


class PoolState:
    def __init__(self, mint, pool, kind, subs):
        self.mint, self.pool, self.kind, self.subs = mint, pool, kind, subs   # subs: {address: role}
        self.coin = self.quote = self.real_quote = None
        self.ts = None
        self.hist = deque()
        self.trusted = None          # None = not checked yet against DexScreener; False = poll only
        self.diff_pct = None
        self.dex_native = self.quote_usd = None
        self.creator = None
        self.dev_start = self.dev_now = None
        self.dev_supply_pct = None
        self.updates = 0

    @property
    def price_native(self):
        if self.coin and self.quote and self.coin > 0 and self.quote > 0:
            return self.quote / self.coin
        return None

    @property
    def quote_reserve(self):
        return self.real_quote if self.kind == "pumpfun" else self.quote

    def record(self, now):
        self.ts = now
        self.updates += 1
        q = self.quote_reserve
        if q is not None:
            self.hist.append((now, q))
        while self.hist and now - self.hist[0][0] > HIST_KEEP_S and len(self.hist) > 1:
            self.hist.popleft()


async def _default_connect(url):
    from websockets.asyncio.client import connect
    return connect(url, ping_interval=20, ping_timeout=20, max_size=2 ** 22, open_timeout=15)


class Watcher:
    """One websocket for all open positions. Reconnects with 1→60 s backoff and resubscribes everything."""

    def __init__(self, ws_url, rpc, on_update=None, clock=time.time, connect=None, sleep=asyncio.sleep):
        self.url, self.rpc, self.on_update, self.clock, self.sleep = ws_url, rpc, on_update, clock, sleep
        self.connect = connect
        self.pools = {}                 # mint -> PoolState
        self.by_addr = {}               # address -> (mint, role)
        self.sub_ids = {}               # ws subscription id -> address
        self.addr_sub = {}              # address -> ws subscription id
        self.pending = {}               # request id -> address
        self.unsupported = {}           # mint -> reason (stays on DexScreener polling)
        self.ws = None
        self.connected = False
        self.stopped = False
        self.next_id = 1
        self.reconnects = 0

    def state(self, mint):
        return self.pools.get(mint)

    def note_dex(self, mint, price_usd, price_native):
        st = self.pools.get(mint)
        if st and price_usd and price_native:
            st.dex_native, st.quote_usd = float(price_native), float(price_usd) / float(price_native)

    async def _rpc_value(self, method, params):
        r = await self.rpc(method, params)
        return (r or {}).get("result", {}).get("value") if isinstance((r or {}).get("result"), dict) else (r or {}).get("result")

    async def watch(self, mint, pool, creator=None):
        """Decode the pool and subscribe. -> PoolState, or None (unsupported: Risk polls DexScreener)."""
        if mint in self.pools:
            return self.pools[mint]
        v = await self._rpc_value("getAccountInfo", [pool, {"encoding": "base64"}])
        if not v:
            self.unsupported[mint] = "pool account not found"
            return None
        d = decode_pool(v.get("owner"), b64data(v), mint, pool)
        if not d:
            self.unsupported[mint] = f"pool type not supported ({(v.get('owner') or '?')[:8]})"
            log.info("%s %s -> DexScreener polling", mint, self.unsupported[mint])
            return None
        if d["kind"] == "pumpfun":
            st = PoolState(mint, pool, "pumpfun", {pool: "curve"})
            self._apply(st, "curve", v)
        else:
            st = PoolState(mint, pool, d["kind"], {d["coin_vault"]: "coin", d["quote_vault"]: "quote"})
            vals = await self._rpc_value("getMultipleAccounts", [[d["coin_vault"], d["quote_vault"]], {"encoding": "jsonParsed"}])
            for addr, val in zip((d["coin_vault"], d["quote_vault"]), vals or []):
                if val:
                    self._apply(st, st.subs[addr], val)
            if st.price_native is None:
                self.unsupported[mint] = "vault balances unreadable"
                return None
        st.record(self.clock())
        st.creator = d.get("creator") or creator
        if st.creator:
            await self._watch_dev(st)
        self.pools[mint] = st
        for addr, role in st.subs.items():
            self.by_addr[addr] = (mint, role)
            if self.connected:
                await self._subscribe(addr, role)
        log.info("%s on-chain %s via %d accounts", mint, st.kind, len(st.subs))
        return st

    async def _watch_dev(self, st):
        """Creator's token account for this coin, only when it holds something (skipped on any RPC error)."""
        try:
            accts = await self._rpc_value("getTokenAccountsByOwner", [st.creator, {"mint": st.mint}, {"encoding": "jsonParsed"}])
            best = max(((a["pubkey"], token_ui(a["account"]) or 0) for a in accts or []), key=lambda x: x[1], default=None)
            if not best or best[1] <= 0:
                return
            sup = await self._rpc_value("getTokenSupply", [st.mint])
            total = float(sup.get("uiAmountString") or sup.get("uiAmount") or 0) if sup else 0
            st.dev_start = st.dev_now = best[1]
            st.dev_supply_pct = best[1] / total * 100 if total else None
            st.subs[best[0]] = "dev"
        except Exception as e:
            log.info("%s creator wallet not watched (%s)", st.mint, type(e).__name__)

    def unwatch(self, mint):
        st = self.pools.pop(mint, None)
        if not st:
            return
        for addr in st.subs:
            self.by_addr.pop(addr, None)
            sid = self.addr_sub.pop(addr, None)
            if sid is not None:
                self.sub_ids.pop(sid, None)
                if self.connected:
                    asyncio.ensure_future(self._send({"jsonrpc": "2.0", "id": self._id(), "method": "accountUnsubscribe", "params": [sid]}))

    def _id(self):
        self.next_id += 1
        return self.next_id

    async def _send(self, obj):
        try:
            await self.ws.send(json.dumps(obj))
        except Exception as e:
            log.info("ws send failed (%s)", type(e).__name__)

    async def _subscribe(self, addr, role):
        i = self._id()
        self.pending[i] = addr
        enc = "base64" if role == "curve" else "jsonParsed"
        await self._send({"jsonrpc": "2.0", "id": i, "method": "accountSubscribe",
                          "params": [addr, {"encoding": enc, "commitment": "processed"}]})

    def _apply(self, st, role, value):
        if role == "curve":
            cur = decode_curve(b64data(value))
            if cur:
                st.coin, st.quote, st.real_quote = cur["coin"], cur["quote"], cur["real_quote"]
            return
        amt = token_ui(value)
        if amt is None:
            return
        if role == "coin":
            st.coin = amt
        elif role == "quote":
            st.quote = amt
        elif role == "dev":
            st.dev_now = amt

    async def handle(self, msg):
        if "id" in msg and msg.get("id") in self.pending:
            addr = self.pending.pop(msg["id"])
            if "result" in msg and addr in self.by_addr:
                self.sub_ids[msg["result"]] = addr
                self.addr_sub[addr] = msg["result"]
            elif "error" in msg:
                log.warning("subscribe failed: %s", (msg["error"] or {}).get("message"))
            return None
        if msg.get("method") != "accountNotification":
            return None
        prm = msg.get("params") or {}
        addr = self.sub_ids.get(prm.get("subscription"))
        if not addr or addr not in self.by_addr:
            return None
        mint, role = self.by_addr[addr]
        st = self.pools.get(mint)
        if not st:
            return None
        self._apply(st, role, (prm.get("result") or {}).get("value") or {})
        st.record(self.clock())
        if self.on_update:
            await self.on_update(mint, st, role)
        return mint

    async def run(self):
        backoff = 1
        while not self.stopped:
            if not self.pools:          # no open position: no socket
                await self.sleep(1)
                continue
            try:
                cm = self.connect(self.url) if self.connect else await _default_connect(self.url)
                async with cm as ws:
                    self.ws, self.connected = ws, True
                    self.sub_ids.clear(); self.addr_sub.clear(); self.pending.clear()
                    log.info("ws connected (%d pools)", len(self.pools))
                    for addr, (_m, role) in list(self.by_addr.items()):
                        await self._subscribe(addr, role)
                    backoff = 1
                    async for raw in ws:
                        try:
                            await self.handle(json.loads(raw))
                        except Exception:
                            log.exception("ws message failed")
                        if self.stopped or not self.pools:
                            break
            except Exception as e:
                log.warning("ws down (%s); retry in %ss", type(e).__name__, backoff)
            finally:
                self.connected, self.ws = False, None
            if self.stopped:
                break
            self.reconnects += 1
            await self.sleep(backoff)
            backoff = min(backoff * 2, 60)
