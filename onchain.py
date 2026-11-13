"""onchain.py — real-time pool prices for open positions from a Solana RPC websocket (accountSubscribe).

Supported pools (owner program of the DexScreener pairAddress, layouts checked live 2026-11-12):
  pumpfun      6EF8rr… bonding curve account: virtual token / SOL reserves (u64 @8 / @16), real SOL @32
  pumpswap     pAMMBay… pool: base_mint @43, quote_mint @75, base vault @139, quote vault @171, coin_creator @211,
               is_mayhem_mode @243, is_cashback_coin @244, virtual_quote_reserves i128 @245 (signed; may be negative)
               PumpSwap prices against effective quote = quote vault + virtual_quote_reserves (pump-public-docs);
               the raw vault alone read 13-60% under the swap price on boosted pools (checked live 2026-11-13).
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
RAY_V4_AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
RAY_CPMM_AUTH = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"
# Token-account owners that are never "the largest holder": AMM programs / authorities, token programs, system.
NOT_HOLDERS = {PUMP_FUN, PUMP_SWAP, RAY_V4, RAY_CPMM, RAY_V4_AUTH, RAY_CPMM_AUTH,
               "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
               "11111111111111111111111111111111", "1nc1nerator11111111111111111111111111111111"}
TOP_CANDIDATES = 10
TOP_RETRY_S = (5, 15)            # backoff between getTokenLargestAccounts attempts on 429 / timeout
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


_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def on_curve(addr) -> bool:
    """True when a base58 address is a point on ed25519 (a wallet). PDAs (pool vault authorities, bonding
    curves, lockers, any program-owned account) are off the curve and are never counted as a holder."""
    try:
        b = b58decode(addr)
    except ValueError:
        return False
    if len(b) != 32:
        return False
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return not (b[31] >> 7)
    return pow(x2, (_P - 1) // 2, _P) == 1


def pick_top_holder(largest, owners, exclude_accounts=(), exclude_owners=()):
    """largest: getTokenLargestAccounts value [{address, uiAmount(String)}]; owners: {token account: owner}.
    -> (token_account, owner, amount) of the biggest holder that is a wallet (not the pool, a vault,
    a program or a PDA), or None. The creator can be it."""
    skip_a, skip_o = set(exclude_accounts), set(exclude_owners) | NOT_HOLDERS
    for a in largest or []:
        addr = a.get("address")
        try:
            amt = float(a.get("uiAmountString") or a.get("uiAmount") or 0)
        except (TypeError, ValueError):
            continue
        own = owners.get(addr)
        if not addr or amt <= 0 or not own or addr in skip_a or own in skip_o or not on_curve(own):
            continue
        return addr, own, amt
    return None


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
    out = {"kind": kind, "coin_vault": coin_vault, "quote_vault": quote_vault, "quote_mint": quote_mint, "creator": creator}
    if kind == "pumpswap":
        out["vq_raw"] = virtual_quote_raw(data)
    return out


def virtual_quote_raw(data: bytes) -> int:
    """PumpSwap Pool::virtual_quote_reserves (i128 little-endian @245, raw quote units). 0 on older/short layouts."""
    if not data or len(data) < 261:
        return 0
    return int.from_bytes(data[245:261], "little", signed=True)


def token_decimals(value, default=9):
    try:
        return int(value["data"]["parsed"]["info"]["tokenAmount"]["decimals"])
    except (KeyError, TypeError, ValueError):
        return default


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


ENTRY_RPC_TIMEOUT_S = 3.0
ENTRY_RETRY_S = 0.4              # one quick retry after a 429 / timeout before the buy falls back to DexScreener


async def _entry_rpc(rpc, method, params, sleep, timeout):
    """One RPC call for the entry read: short timeout, one quick retry on 429 / timeout. Raises on failure."""
    last = None
    for i in range(2):
        try:
            try:
                coro = rpc(method, params, timeout=timeout)
            except TypeError:                      # fakes without a timeout kwarg
                coro = rpc(method, params)
            r = await asyncio.wait_for(coro, timeout + 0.5)
            res = (r or {}).get("result")
            return res.get("value") if isinstance(res, dict) else res
        except Exception as e:
            last = e
            status = getattr(e, "status", None)
            transient = (status in (429, -32429) or isinstance(e, asyncio.TimeoutError)
                         or "Timeout" in type(e).__name__ or "Timeout" in str(e))
            if not transient or i:
                raise
            await sleep(ENTRY_RETRY_S)
    raise last


async def read_price(rpc, pool, mint, sleep=asyncio.sleep, timeout=ENTRY_RPC_TIMEOUT_S):
    """Executor: the pool's on-chain price once, just before a buy (same decoders as the Watcher).
    pump.fun curve = 1 call (getAccountInfo); vault pools = getAccountInfo + one getMultipleAccounts for both vaults.
    -> ({kind, price_native, quote_reserve}, None) or (None, reason) when unsupported / unreadable."""
    if not rpc or not pool:
        return None, "no RPC / pool"
    try:
        v = await _entry_rpc(rpc, "getAccountInfo", [pool, {"encoding": "base64", "commitment": "processed"}], sleep, timeout)
    except Exception as e:
        return None, f"RPC {type(e).__name__}{' ' + str(getattr(e, 'status', '')) if getattr(e, 'status', None) is not None else ''}"
    if not v:
        return None, "pool account not found"
    d = decode_pool(v.get("owner"), b64data(v), mint, pool)
    if not d:
        return None, f"pool type not supported ({(v.get('owner') or '?')[:8]})"
    if d["kind"] == "pumpfun":
        cur = decode_curve(b64data(v))
        if not cur or cur["coin"] <= 0 or cur["quote"] <= 0:
            return None, "curve unreadable"
        return {"kind": "pumpfun", "price_native": cur["quote"] / cur["coin"], "quote_reserve": cur["real_quote"]}, None
    try:
        vals = await _entry_rpc(rpc, "getMultipleAccounts",
                                [[d["coin_vault"], d["quote_vault"]], {"encoding": "jsonParsed", "commitment": "processed"}],
                                sleep, timeout)
    except Exception as e:
        return None, f"RPC {type(e).__name__}"
    vals = list(vals or []) + [None, None]
    coin, quote = token_ui(vals[0]) if vals[0] else None, token_ui(vals[1]) if vals[1] else None
    if not coin or not quote or coin <= 0 or quote <= 0:
        return None, "vault balances unreadable"
    eff = quote + d.get("vq_raw", 0) / 10 ** token_decimals(vals[1])
    if eff <= 0:
        return None, "effective quote reserve <= 0"
    return {"kind": d["kind"], "price_native": eff / coin, "quote_reserve": quote}, None


class PoolState:
    def __init__(self, mint, pool, kind, subs):
        self.mint, self.pool, self.kind, self.subs = mint, pool, kind, subs   # subs: {address: role}
        self.coin = self.quote = self.real_quote = None
        self.vq_raw, self.quote_decimals = 0, 9   # PumpSwap virtual quote reserves (raw) + quote token decimals
        self.ts = None
        self.hist = deque()
        self.trusted = None          # None = not checked yet against DexScreener; False = poll only
        self.diff_pct = None
        self.gap_exit = None         # set by Risk.ws_price: distrusted and zincir far under DexScreener
        self.dex_native = self.quote_usd = None
        self.creator = None
        self.dev_start = self.dev_now = None
        self.dev_supply_pct = None
        self.top_account = self.top_owner = None
        self.top_start = self.top_now = None   # largest wallet's balance when watching started / now
        self.updates = 0

    @property
    def effective_quote(self):
        """Quote side the AMM prices against: vault balance + PumpSwap virtual_quote_reserves (0 elsewhere)."""
        if self.quote is None:
            return None
        return self.quote + (self.vq_raw or 0) / 10 ** (self.quote_decimals or 0)

    @property
    def price_native(self):
        q = self.effective_quote
        if self.coin and q and self.coin > 0 and self.quote > 0 and q > 0:
            return q / self.coin
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

    def __init__(self, ws_url, rpc, on_update=None, clock=time.time, connect=None, sleep=asyncio.sleep, holders=None):
        self.url, self.rpc, self.on_update, self.clock, self.sleep = ws_url, rpc, on_update, clock, sleep
        self.holders = holders          # async mint -> (largest, owners, pool accounts): RugCheck fallback
        self.connect = connect
        self.pools = {}                 # mint -> PoolState
        self.by_addr = {}               # address -> (mint, role)
        self.sub_ids = {}               # ws subscription id -> address
        self.addr_sub = {}              # address -> ws subscription id
        self.pending = {}               # request id -> address
        self.unsupported = {}           # mint -> reason (no on-chain price: DexScreener polling)
        self.top_failed = {}            # mint -> reason (largest holder not watched)
        self.top_tasks = {}
        self._top_warned = False
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
            return await self._holder_only(mint, pool)
        d = decode_pool(v.get("owner"), b64data(v), mint, pool)
        if not d:
            self.unsupported[mint] = f"pool type not supported ({(v.get('owner') or '?')[:8]})"
            log.info("%s %s -> DexScreener polling", mint, self.unsupported[mint])
            return await self._holder_only(mint, pool)
        if d["kind"] == "pumpfun":
            st = PoolState(mint, pool, "pumpfun", {pool: "curve"})
            self._apply(st, "curve", v)
        else:
            st = PoolState(mint, pool, d["kind"], {d["coin_vault"]: "coin", d["quote_vault"]: "quote"})
            if d["kind"] == "pumpswap":           # virtual_quote_reserves moves with every trade's fees
                st.vq_raw = d.get("vq_raw", 0)
                st.subs[pool] = "pool"
            vals = await self._rpc_value("getMultipleAccounts", [[d["coin_vault"], d["quote_vault"]], {"encoding": "jsonParsed"}])
            for addr, val in zip((d["coin_vault"], d["quote_vault"]), vals or []):
                if val:
                    self._apply(st, st.subs[addr], val)
            if st.price_native is None:
                self.unsupported[mint] = "vault balances unreadable"
                return await self._holder_only(mint, pool)
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
        self._start_top(st)
        return st

    async def _holder_only(self, mint, pool):
        """No on-chain price for this pool, but still watch its largest wallet (Risk prices it from
        DexScreener). -> None so callers keep treating the pool as unsupported for prices."""
        st = PoolState(mint, pool, "holder", {})
        self.pools[mint] = st
        self._start_top(st)
        return None

    def _start_top(self, st):
        try:
            self.top_tasks[st.mint] = asyncio.ensure_future(self._watch_top(st))
        except RuntimeError:            # no running loop (sync callers / tests)
            pass

    async def _watch_top(self, st):
        """One lookup at entry: getTokenLargestAccounts + owners (getMultipleAccounts jsonParsed), skipping the
        pool's vaults / curve and every program-owned account; then the winner's token account rides the same
        websocket (role 'top'). Public RPC 429s getTokenLargestAccounts, so after a 429 / timeout the RugCheck
        topHolders list (self.holders) is tried, then the RPC again after 5 s and 15 s; then given up for this coin."""
        reason = None
        tried_rc = False
        for i in range(len(TOP_RETRY_S) + 1):
            try:
                got = await self._top_from_rpc(st)
                if got is None:
                    reason = "no wallet among the largest holders"
                    break
                return await self._adopt_top(st, got, "rpc")
            except Exception as e:
                status = getattr(e, "status", None)
                reason = f"{type(e).__name__}{' ' + str(status) if status is not None else ''}"
                transient = status in (429, -32429) or "Timeout" in type(e).__name__ or "Timeout" in str(e)
                if transient and self.holders and not tried_rc:
                    tried_rc = True
                    try:
                        got = await self._top_from_rugcheck(st)
                        if got:
                            return await self._adopt_top(st, got, "rugcheck")
                    except Exception as e2:
                        reason += f"; rugcheck {type(e2).__name__}"
                if not transient or i >= len(TOP_RETRY_S):
                    break
                await self.sleep(TOP_RETRY_S[i])
        self.top_failed[st.mint] = reason
        if not self._top_warned:
            self._top_warned = True
            log.warning("top holder not watched (%s); later failures log at info", reason)
        else:
            log.info("%s top holder not watched (%s)", st.mint, reason)
        return None

    def _top_skip(self, st):
        return [a for a, r in st.subs.items() if r != "dev"], [st.pool, st.mint]

    async def _top_from_rpc(self, st):
        largest = await self._rpc_value("getTokenLargestAccounts", [st.mint, {"commitment": "confirmed"}])
        cands = [a for a in largest or [] if a.get("address")][:TOP_CANDIDATES]
        vals = await self._rpc_value("getMultipleAccounts", [[a["address"] for a in cands], {"encoding": "jsonParsed"}]) if cands else []
        owners = {}
        for a, val in zip(cands, vals or []):
            try:
                owners[a["address"]] = val["data"]["parsed"]["info"]["owner"]
            except (KeyError, TypeError):
                pass
        ex_a, ex_o = self._top_skip(st)
        return pick_top_holder(cands, owners, exclude_accounts=ex_a, exclude_owners=ex_o)

    async def _top_from_rugcheck(self, st):
        """self.holders(mint) -> (largest [{address, uiAmountString}], {account: owner}, pool accounts to skip).
        The balance snapshot is re-read from the chain (one getAccountInfo) so a cached report is not the baseline."""
        largest, owners, pool_accts = await self.holders(st.mint)
        ex_a, ex_o = self._top_skip(st)
        got = pick_top_holder(largest[:TOP_CANDIDATES], owners, exclude_accounts=ex_a + list(pool_accts),
                              exclude_owners=ex_o + list(pool_accts))
        if not got:
            return None
        try:
            v = await self._rpc_value("getAccountInfo", [got[0], {"encoding": "jsonParsed"}])
            amt = token_ui(v) if v else None
            if amt is not None and amt > 0:
                got = (got[0], got[1], amt)
        except Exception:
            pass
        return got

    async def _adopt_top(self, st, got, via):
        if st.mint not in self.pools or self.pools[st.mint] is not st:
            return None              # closed while we looked
        addr, owner, amt = got
        st.top_account, st.top_owner, st.top_start, st.top_now = addr, owner, amt, amt
        st.top_via = via
        role = "dev+top" if st.subs.get(addr) == "dev" else "top"
        st.subs[addr] = role
        self.by_addr[addr] = (st.mint, role)
        if self.connected and addr not in self.addr_sub:     # the creator's account is already subscribed
            await self._subscribe(addr, role)
        log.info("%s top holder %s… watched via %s (%.0f tokens)", st.mint, owner[:6], via, amt)
        return got

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
        t = self.top_tasks.pop(mint, None)
        if t and not t.done():
            t.cancel()
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
        enc = "base64" if role in ("curve", "pool") else "jsonParsed"
        await self._send({"jsonrpc": "2.0", "id": i, "method": "accountSubscribe",
                          "params": [addr, {"encoding": enc, "commitment": "processed"}]})

    def _apply(self, st, role, value):
        if role == "curve":
            cur = decode_curve(b64data(value))
            if cur:
                st.coin, st.quote, st.real_quote = cur["coin"], cur["quote"], cur["real_quote"]
            return
        if role == "pool":
            data = b64data(value)
            if data and len(data) >= 261:
                st.vq_raw = virtual_quote_raw(data)
            return
        amt = token_ui(value)
        if amt is None:
            return
        if role == "coin":
            st.coin = amt
        elif role == "quote":
            st.quote = amt
            st.quote_decimals = token_decimals(value, st.quote_decimals)
        if role in ("dev", "dev+top"):
            st.dev_now = amt
        if role in ("top", "dev+top"):
            st.top_now = amt

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
