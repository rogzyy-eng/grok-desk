"""On-chain price (accountSubscribe) + rug early warning, all with mocked websocket / RPC data."""
import asyncio
import base64
import json
import struct

import commands
import config
import onchain
from agents.risk import Risk, rug_early
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier
from onchain import PoolState, Watcher, b58decode, b58encode, decode_curve, decode_pool, fast_drain

MINT = "GMk6j2defJhS7F194toqmJNFNhAkbDXhYJo5oR3Rpump"
WSOL = "So11111111111111111111111111111111111111112"
CV, QV, CR = "DAttZjH2QdSnceeo2gvLeD9t5nS1MdsjhyF2GGngvXEr", "2aqJKE5ydU2ZKCkhDyDmjkewmf5nkSjLiLDD34rMErYQ", "7yuqVEF2ZX8iFz799efzQEsgMwGhf8vZDaHLuTbQxsFx"


def put(buf, off, addr):
    buf[off:off + 32] = b58decode(addr)


def pumpswap_bytes(base=MINT, quote=WSOL, bv=CV, qv=QV, creator=CR):
    b = bytearray(301)
    put(b, 43, base); put(b, 75, quote); put(b, 139, bv); put(b, 171, qv); put(b, 211, creator)
    return bytes(b)


def curve_bytes(vtok=489_714_535_517_274, vsol=65_732_172_337, rsol=35_732_172_337, complete=0):
    b = bytearray(150)
    struct.pack_into("<QQQQQ?", b, 8, vtok, vsol, 0, rsol, 10 ** 15, bool(complete))
    return bytes(b)


def tok(amount):
    return {"data": {"parsed": {"info": {"tokenAmount": {"uiAmountString": str(amount), "decimals": 6}}}, "program": "spl-token"}}


def acct(owner, data):
    return {"owner": owner, "data": [base64.b64encode(data).decode(), "base64"]}


# ---- decoders ----
def test_b58_roundtrip():
    for a in (MINT, WSOL, "11111111111111111111111111111111"):
        assert b58encode(b58decode(a)) == a and len(b58decode(a)) == 32


def test_decode_pumpswap_both_orders():
    d = decode_pool(onchain.PUMP_SWAP, pumpswap_bytes(), MINT)
    assert d == {"kind": "pumpswap", "coin_vault": CV, "quote_vault": QV, "quote_mint": WSOL, "creator": CR, "vq_raw": 0}
    d = decode_pool(onchain.PUMP_SWAP, pumpswap_bytes(base=WSOL, quote=MINT, bv=QV, qv=CV), MINT)
    assert (d["coin_vault"], d["quote_vault"], d["quote_mint"]) == (CV, QV, WSOL)
    assert decode_pool(onchain.PUMP_SWAP, pumpswap_bytes(), "OtherMint1111111111111111111111111111111111") is None
    assert decode_pool("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", pumpswap_bytes(), MINT) is None   # CLMM -> poll
    assert decode_pool(onchain.PUMP_SWAP, b"\0" * 100, MINT) is None


def test_decode_raydium_layouts():
    v4 = bytearray(752); put(v4, 336, CV); put(v4, 368, QV); put(v4, 400, MINT); put(v4, 432, WSOL)
    assert decode_pool(onchain.RAY_V4, bytes(v4), MINT)["coin_vault"] == CV
    cp = bytearray(637); put(cp, 72, QV); put(cp, 104, CV); put(cp, 168, WSOL); put(cp, 200, MINT)
    d = decode_pool(onchain.RAY_CPMM, bytes(cp), MINT)
    assert (d["kind"], d["coin_vault"], d["quote_vault"]) == ("raydium_cpmm", CV, QV)


def test_decode_curve_price_matches_live_sample():
    c = decode_curve(curve_bytes())
    assert abs(c["quote"] / c["coin"] - 1.342e-7) / 1.342e-7 < 0.01      # DexScreener priceNative 0.0000001341
    assert decode_pool(onchain.PUMP_FUN, curve_bytes(complete=1), MINT, "CURVE") is None    # migrated curve
    assert decode_pool(onchain.PUMP_FUN, curve_bytes(), MINT, "CURVE")["curve"] == "CURVE"


def test_fast_drain_window_and_baseline():
    assert fast_drain([(0, 100), (5, 90), (10, 74)], 10, 20, 25) == -26.0
    assert fast_drain([(0, 100), (15, 90), (30, 80), (45, 70)], 45, 20, 25) is None      # slow bleed: not a rug
    assert round(fast_drain([(0, 100), (100, 40)], 100, 20, 25)) == -60                   # quiet minute then one pull
    assert fast_drain([], 0, 20, 25) is None


# ---- watcher with a fake websocket ----
class FakeRPC:
    def __init__(self, pool_acct, vaults, creator_amt=0.0, supply=1e9):
        self.pool_acct, self.vaults, self.creator_amt, self.supply = pool_acct, vaults, creator_amt, supply
        self.calls = []

    async def __call__(self, method, params):
        self.calls.append(method)
        if method == "getAccountInfo":
            return {"result": {"value": self.pool_acct}}
        if method == "getMultipleAccounts":
            return {"result": {"value": [tok(self.vaults[a]) for a in params[0]]}}
        if method == "getTokenAccountsByOwner":
            return {"result": {"value": [{"pubkey": "DEVATA", "account": tok(self.creator_amt)}] if self.creator_amt else []}}
        if method == "getTokenSupply":
            return {"result": {"value": {"uiAmountString": str(self.supply)}}}
        raise AssertionError(method)


class FakeWS:
    def __init__(self, notes, then=None):
        self.sent, self.notes, self.then = [], notes, then

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, s):
        self.sent.append(json.loads(s))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        subs = {}
        for m in list(self.sent):
            if m["method"] == "accountSubscribe":
                subs[m["params"][0]] = 1000 + m["id"]
                yield json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": 1000 + m["id"]})
        for addr, value in self.notes:
            yield json.dumps({"jsonrpc": "2.0", "method": "accountNotification",
                              "params": {"subscription": subs[addr], "result": {"context": {"slot": 1}, "value": value}}})
        if self.then:
            raise self.then


def test_watcher_subscribes_prices_and_reconnects():
    rpc = FakeRPC(acct(onchain.PUMP_SWAP, pumpswap_bytes()), {CV: 1_000_000, QV: 100}, creator_amt=50_000_000)
    seen = []
    w = None
    sockets = [FakeWS([(QV, tok(110))], then=ConnectionError("drop")), FakeWS([(CV, tok(900_000)), ("DEVATA", tok(0))])]
    conns = []

    def connect(url):
        assert url == "wss://example.invalid/?api-key=secret"
        ws = sockets[len(conns)]
        conns.append(ws)
        return ws

    async def sleep(_s):
        if len(conns) == 2:
            w.stopped = True

    async def on_update(mint, st, role):
        seen.append((role, round(st.price_native, 8)))

    async def go():
        nonlocal w
        w = Watcher("wss://example.invalid/?api-key=secret", rpc, on_update, clock=lambda: 50.0, connect=connect, sleep=sleep)
        st = await w.watch(MINT, "POOL")
        assert st.kind == "pumpswap" and st.price_native == 100 / 1_000_000 and st.dev_supply_pct == 5.0
        await w.run()
        return st

    st = asyncio.run(go())
    assert seen == [("quote", 0.00011), ("coin", round(110 / 900_000, 8)), ("dev", round(110 / 900_000, 8))]
    assert w.reconnects == 2 and len(conns) == 2
    subs = [m["params"][0] for ws in conns for m in ws.sent if m["method"] == "accountSubscribe"]
    assert sorted(subs) == sorted([CV, QV, "DEVATA", "POOL"] * 2)  # resubscribed after the drop (pool = virtual quote)
    assert st.dev_now == 0 and st.dev_start == 50_000_000


def test_unknown_pool_stays_on_polling():
    rpc = FakeRPC({"owner": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "data": ["AAAA", "base64"]}, {})
    w = Watcher("wss://x", rpc)
    assert asyncio.run(w.watch(MINT, "POOL")) is None and "not supported" in w.unsupported[MINT]


# ---- risk on chain prices ----
def mk(db, t=1000.0):
    bus = Bus(db)
    n = Notifier(db, bus)
    r = Risk(db, bus, FakeSrc(price=1.0, liq=50000), n, clock=lambda: t, sleep=no_sleep)
    r.watcher = Watcher("wss://x", None)
    r.watcher.connected = True
    return r, n


def open_pos(db, mint="M"):
    db.insert("coins", {"mint": mint, "symbol": mint, "pool": "POOL"})
    db.set("risk.stop_pct", 15)
    return db.insert("positions", {"mint": mint, "mode": "PAPER", "opened_at": 900.0, "entry_price": 1.0, "size_usd": 10,
                                   "qty": 9.8, "entry_fee": 0.18, "fees_usd": 0.18, "peak_price": 1.0, "stop_pct": 15,
                                   "stop_price": 0.85, "trail_pct": 20, "trail_start_pct": 30, "timeout_h": 3,
                                   "runner_trail_pct": 30, "status": "open", "entry_liq_usd": 50000})


def state(mint="M", coin=25000.0, quote=25.0, dex_native=0.001, usd=1000.0, t=1000.0):
    st = PoolState(mint, "POOL", "pumpswap", {"CV": "coin", "QV": "quote"})
    st.coin, st.quote, st.dex_native, st.quote_usd = coin, quote, dex_native, usd
    st.record(t)
    return st


def test_ws_stop_fires_on_update_with_source(db):
    r, n = mk(db)
    open_pos(db)
    st = state()
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))                     # price 1.0: checked + trusted, hold
    assert st.trusted is True and db.one("SELECT reason FROM decisions WHERE decision='ws_check'")["reason"].startswith(
        "M: zincir fiyatı DexScreener'a göre +0.0% (pumpswap) · anlık zincir")
    st.quote = 23.0; st.coin = 26000.0; st.record(1000.0)       # 0.000885 SOL * 1000 = $0.885 → still above the -15% stop
    asyncio.run(r.on_ws("M", st, "quote"))
    assert db.one("SELECT status FROM positions")["status"] == "open"
    st.quote = 22.5; st.coin = 26750.0; st.record(1000.0)        # $0.841 → stop at the on-chain price, no 5 s wait
    asyncio.run(r.on_ws("M", st, "quote"))
    p = db.one("SELECT * FROM positions")
    assert p["status"] == "closed" and p["exit_reason"] == "stop" and p["price_source"] == "zincir"
    assert abs(p["exit_price"] - 22.5 / 26750 * 1000) < 1e-9
    assert any("fiyat: zincir" in s["body"] for s in n.sent if s.get("body"))


def test_ws_distrusted_when_far_from_dexscreener(db):
    r, n = mk(db)
    open_pos(db)
    st = state(dex_native=0.0005, usd=500.0)                   # ws says 2x DexScreener: decode suspect
    r.watcher.pools["M"] = st                                  # ($0.50 = a -50% stop if it were trusted)
    asyncio.run(r.on_ws("M", st, "coin"))
    assert st.trusted is False and db.one("SELECT status FROM positions")["status"] == "open"
    assert "güvenilmedi, DexScreener ile devam" in db.one("SELECT reason FROM decisions WHERE decision='ws_check'")["reason"]
    out = asyncio.run(r.tick())                                # falls back to the DexScreener poll ($1.00, hold)
    assert out == [] and db.one("SELECT price_source FROM positions")["price_source"] == "DexScreener"


def test_rug_early_on_fast_liquidity_pull(db):
    r, n = mk(db)
    open_pos(db)
    st = state(t=990.0)
    r.watcher.pools["M"] = st
    st.quote, st.coin = 17.5, 17500.0                            # LP pulled: both sides -30%, price unchanged
    st.record(1000.0)
    asyncio.run(r.on_ws("M", st, "quote"))
    p = db.one("SELECT * FROM positions")
    assert p["status"] == "closed" and p["exit_reason"] == "rug_early"
    a = db.one("SELECT * FROM activity WHERE mint='M' AND kind='sold'")
    assert a["subtitle"].startswith("Risk · rug erken uyarı (havuzdaki SOL -30% / 20sn)") and "fiyat: zincir" in a["subtitle"]
    assert any(s["body"].startswith("PAPER · M: rug erken uyarısı · hemen sat") for s in n.sent if s.get("body"))


def test_rug_early_dev_sell_and_thresholds():
    c = {"rug_window_s": 20, "rug_liq_drop_fast_pct": 25, "rug_dev_sell_pct": 50, "rug_dev_min_supply_pct": 1}
    st = state()
    assert rug_early(st, 1000.0, c) is None
    st.dev_start, st.dev_now, st.dev_supply_pct = 40e6, 10e6, 4.0
    assert rug_early(st, 1000.0, c) == "geliştirici cüzdanı payının %75'ini sattı"
    st.dev_supply_pct = 0.2                                     # dust holder: ignored
    assert rug_early(st, 1000.0, c) is None


def test_poll_mode_and_disconnect_use_dexscreener(db):
    r, n = mk(db)
    open_pos(db)
    st = state(); st.trusted = True; st.quote = 12.5           # ws would stop it
    r.watcher.pools["M"] = st
    db.set("risk.price_source", "poll")
    assert asyncio.run(r.tick()) == [] and "M" not in r.watcher.pools   # poll mode unwatches
    db.set("risk.price_source", "auto")
    r.watcher.pools["M"] = st
    r.watcher.connected = False                                 # socket down -> DexScreener
    assert asyncio.run(r.tick()) == []
    assert db.one("SELECT price_source FROM positions")["price_source"] == "DexScreener"


def test_settings_defaults_choices_and_urls(db, monkeypatch):
    s = db.all_settings()["risk"]
    assert (s["price_source"], s["ws_max_diff_pct"], s["rug_liq_drop_fast_pct"], s["rug_window_s"]) == ("auto", 20, 25, 20)
    assert (s["rug_dev_sell_pct"], s["rug_dev_min_supply_pct"]) == (50, 1)
    assert db.get("desk.ws_url") == "wss://api.mainnet-beta.solana.com"
    for k in ("risk.ws_max_diff_pct", "risk.rug_liq_drop_fast_pct", "risk.rug_window_s", "risk.rug_dev_sell_pct"):
        assert k in config.RANGES
    commands.set_setting(db, "risk.price_source", "poll", "test")
    try:
        commands.set_setting(db, "risk.price_source", "magic", "test")
        assert False
    except commands.GateError:
        pass
    assert config.ws_url(db) == "wss://api.mainnet-beta.solana.com"
    monkeypatch.setattr(config, "SOLANA_RPC_URL", "https://rpc.example.invalid/?api-key=k")
    assert config.ws_url(db) == "wss://rpc.example.invalid/?api-key=k" and config.rpc_url(db).startswith("https://rpc.")
    monkeypatch.setattr(config, "SOLANA_WS_URL", "wss://ws.example.invalid")
    assert config.ws_url(db) == "wss://ws.example.invalid"
    assert "SOLANA_RPC_URL" in config.SECRET_ENV_NAMES
