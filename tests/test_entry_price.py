"""Entry at the on-chain price (onchain.read_price): gap skip, zincir fill, Dex fallback, ws check vs the fill,
and the distrusted-and-far-under-Dex exit. Mocked RPC only."""
import asyncio

import config
import onchain
from agents.executor import Executor
from agents.risk import Risk
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier
from onchain import PoolState, Watcher, read_price
from sources import FetchError
from test_onchain import CV, MINT, QV, acct, curve_bytes, pumpswap_bytes, tok

POOL = "PoolAddr1111111111111111111111111111111111"


def sol(amount):
    """WSOL vault (9 decimals)."""
    t = tok(amount)
    t["data"]["parsed"]["info"]["tokenAmount"]["decimals"] = 9
    return t


class ChainSrc(FakeSrc):
    """DexScreener fake with priceNative + a scripted Solana RPC (pumpswap pool, vault balances)."""
    def __init__(self, price=0.001, native=0.000001, liq=50000.0, coin=1_000_000.0, quote=1.0, fail=0, status=429,
                 owner=onchain.PUMP_SWAP, **kw):
        super().__init__(price=price, liq=liq, **kw)
        self.native, self.coin, self.quote, self.fail, self.status, self.owner = native, coin, quote, fail, status, owner
        self.calls = []

    async def dex_pairs(self, mints):
        out = await super().dex_pairs(mints)
        for p in out:
            p["pairAddress"], p["priceNative"] = POOL, str(self.native)
        return out

    async def rpc(self, method, params, timeout=30.0):
        self.calls.append((method, timeout))
        if self.fail:
            self.fail -= 1
            raise FetchError("RPC HTTP 429", self.status)
        if method == "getAccountInfo":
            data = pumpswap_bytes(base=MINT) if self.owner == onchain.PUMP_SWAP else curve_bytes()
            return {"result": {"value": acct(self.owner, data)}}
        if method == "getMultipleAccounts":
            return {"result": {"value": [tok(self.coin), sol(self.quote)]}}
        raise AssertionError(method)


def mk_ex(db, src):
    db.insert("coins", {"mint": MINT, "symbol": "TST", "pool": POOL})
    return Executor(db, Bus(db), src, Notifier(db, Bus(db)), sleep=no_sleep)


def ref():
    return {"price_usd": 0.001, "liq_usd": 50000}


# ---- onchain.read_price ----
def test_read_price_pumpswap_and_curve():
    got, why = asyncio.run(read_price(ChainSrc(coin=2_000_000, quote=1.6).rpc, POOL, MINT, sleep=no_sleep))
    assert why is None and got["kind"] == "pumpswap" and abs(got["price_native"] - 8e-7) < 1e-15
    got, _ = asyncio.run(read_price(ChainSrc(owner=onchain.PUMP_FUN).rpc, POOL, MINT, sleep=no_sleep))
    assert got["kind"] == "pumpfun" and abs(got["price_native"] - 1.342e-7) / 1.342e-7 < 0.01


def test_read_price_429_retry_then_give_up():
    s = ChainSrc(fail=1)
    got, why = asyncio.run(read_price(s.rpc, POOL, MINT, sleep=no_sleep))
    assert got and why is None and s.calls[0] == ("getAccountInfo", onchain.ENTRY_RPC_TIMEOUT_S)
    got, why = asyncio.run(read_price(ChainSrc(fail=2).rpc, POOL, MINT, sleep=no_sleep))
    assert got is None and "429" in why
    s = ChainSrc(fail=1, status=-32602)                                   # not transient: no retry
    got, why = asyncio.run(read_price(s.rpc, POOL, MINT, sleep=no_sleep))
    assert got is None and len(s.calls) == 1
    got, why = asyncio.run(read_price(ChainSrc(owner="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK").rpc, POOL, MINT, sleep=no_sleep))
    assert got is None and "not supported" in why


# ---- executor ----
def test_fill_at_onchain_price_sets_lines(db):
    db.set("risk.stop_pct", 6)
    src = ChainSrc(coin=1_000_000, quote=0.88)                            # zincir 12% under Dex: inside 15
    ex = mk_ex(db, src)
    asyncio.run(ex.process({"mint": MINT, "pair": ref()}))
    p = db.open_positions()[0]
    market = 0.00088                                                      # 0.88e-6 SOL × ($0.001 / 1e-6 SOL)
    assert abs(p["market_entry_price"] - market) < 1e-12 and p["price_source"] == "zincir"
    assert abs(p["dex_entry_price"] - 0.001) < 1e-12 and p["entry_gap_pct"] == -12.0
    assert p["entry_price"] > market and p["entry_price"] < market * 1.01  # PAPER impact on top of the zincir price
    assert abs(p["stop_price"] - p["entry_price"] * 0.94) < 1e-12
    assert abs(p["entry_native"] - 8.8e-7) < 1e-15 and abs(p["entry_quote_usd"] - 1000) < 1e-6
    d = db.one("SELECT reason FROM decisions WHERE decision='entry_price'")["reason"]
    assert "-12.0%" in d and "zincir fiyatından girildi" in d
    assert "fiyat: zincir (pumpswap, Dex'e göre -12.0%)" in db.one("SELECT reason FROM signals")["reason"]


def test_gap_skip_and_off(db):
    ex = mk_ex(db, ChainSrc(quote=0.80))                                  # zincir 20% under Dex
    assert asyncio.run(ex.process({"mint": MINT, "pair": ref()})) is None
    assert db.one("SELECT status_reason FROM coins WHERE mint=?", (MINT,))["status_reason"] == "fiyat uyuşmazlığı: zincir %20 düşük → girmedim"
    assert not db.open_positions()
    db.set("executor.entry_max_gap_pct", 0)                               # 0 = off: fill at zincir anyway
    asyncio.run(ex.process({"mint": MINT, "pair": ref()}))
    assert db.open_positions()[0]["price_source"] == "zincir"


def test_zincir_above_dex_fills_at_zincir(db):
    ex = mk_ex(db, ChainSrc(quote=1.05))
    asyncio.run(ex.process({"mint": MINT, "pair": ref()}))
    p = db.open_positions()[0]
    assert abs(p["market_entry_price"] - 0.00105) < 1e-12 and p["entry_gap_pct"] == 5.0


def test_fallback_to_dex_when_unreadable(db):
    ex = mk_ex(db, ChainSrc(fail=5))
    asyncio.run(ex.process({"mint": MINT, "pair": ref()}))
    p = db.open_positions()[0]
    assert p["price_source"] == "DexScreener" and abs(p["market_entry_price"] - 0.001) < 1e-12 and p["entry_native"] is None
    assert "zincir fiyatı okunamadı" in db.one("SELECT reason FROM decisions WHERE decision='entry_price'")["reason"]
    # no rpc on the source at all (tests / poll mode) -> DexScreener, no RPC call
    db.x("DELETE FROM positions")
    ex.src = FakeSrc(price=0.001)
    asyncio.run(ex.process({"mint": MINT, "pair": ref()}))
    assert db.open_positions()[0]["price_source"] == "DexScreener"


def test_poll_mode_skips_entry_read(db):
    db.set("risk.price_source", "poll")
    src = ChainSrc(quote=0.5)
    ex = mk_ex(db, src)
    asyncio.run(ex.process({"mint": MINT, "pair": ref()}))
    assert src.calls == [] and db.open_positions()[0]["price_source"] == "DexScreener"


def test_setting_seeded_with_range(db):
    assert db.get("executor.entry_max_gap_pct") == 15 and config.RANGES["executor.entry_max_gap_pct"] == (0, 100, 1)
    assert db.get("risk.zincir_gap_exit_pct") == 40 and config.RANGES["risk.zincir_gap_exit_pct"] == (0, 100, 1)
    db.set("executor.entry_max_gap_pct", 25)
    db.seed_settings()                                                    # install keeps a live value
    assert db.get("executor.entry_max_gap_pct") == 25


# ---- risk ----
def mk_risk(db, t=1000.0):
    bus = Bus(db)
    n = Notifier(db, bus)
    r = Risk(db, bus, FakeSrc(price=1.0, liq=50000), n, clock=lambda: t, sleep=no_sleep)
    r.watcher = Watcher("wss://x", None)
    r.watcher.connected = True
    return r, n


def pos(db, entry=1.0, entry_native=None, quote_usd=None, liq=50000):
    db.x("INSERT OR IGNORE INTO coins(mint, symbol, pool) VALUES('M','M','POOL')")
    return db.insert("positions", {"mint": "M", "mode": "PAPER", "opened_at": 900.0, "entry_price": entry, "size_usd": 10,
                                   "qty": 9.8 / entry, "entry_fee": 0.18, "fees_usd": 0.18, "peak_price": entry, "stop_pct": 6,
                                   "stop_price": entry * 0.94, "trail_pct": 20, "trail_start_pct": 30, "timeout_h": 3,
                                   "runner_trail_pct": 30, "status": "open", "entry_liq_usd": liq,
                                   "entry_native": entry_native, "entry_quote_usd": quote_usd})


def st_(coin, quote, dex_native=None, usd=None):
    st = PoolState("M", "POOL", "pumpswap", {"CV": "coin", "QV": "quote"})
    st.coin, st.quote, st.dex_native, st.quote_usd = coin, quote, dex_native, usd
    st.record(1000.0)
    return st


def test_zincir_fill_trusted_against_fill_even_if_dex_stale(db):
    r, _ = mk_risk(db)
    pos(db, entry=0.000884, entry_native=8.8e-7, quote_usd=1000.0, liq=1800)        # filled at zincir, Dex was 12-25% higher
    st = st_(1_000_000, 0.875, dex_native=1.2e-6, usd=1000.0)              # ws -27% vs Dex, -0.6% vs the fill
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    p = db.one("SELECT * FROM positions")
    assert st.trusted is True and p["status"] == "open" and p["price_source"] == "zincir"
    d = db.one("SELECT reason FROM decisions WHERE decision='ws_check'")["reason"]
    assert "zincir girişine göre -0.6%" in d and "anlık zincir" in d


def test_zincir_fill_no_dex_yet_uses_fill_quote_usd(db):
    r, _ = mk_risk(db)
    pos(db, entry=0.000884, entry_native=8.8e-7, quote_usd=1000.0, liq=1800)
    st = st_(1_000_000, 0.82)                                               # no DexScreener poll yet; -6.8% vs fill → stop
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    p = db.one("SELECT * FROM positions")
    assert st.quote_usd == 1000.0 and p["status"] == "closed" and p["exit_reason"] == "stop" and p["price_source"] == "zincir"


def test_distrusted_far_under_dex_exits(db):
    r, n = mk_risk(db)
    pos(db, entry=1.0)                                                      # old-style Dex fill
    st = st_(25000.0, 11.5, dex_native=0.001, usd=1000.0)                   # zincir -54% vs Dex (fap)
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    p = db.one("SELECT * FROM positions")
    assert st.trusted is False and p["status"] == "closed" and p["exit_reason"] == "rug_early" and p["price_source"] == "zincir"
    assert abs(p["exit_price"] - 0.46) < 1e-9
    assert any("M: zincir fiyatı %54 düşük → çıktım" in s["body"] for s in n.sent if s.get("body"))


def test_distrusted_mild_gap_rides_dex_and_setting_off(db):
    r, _ = mk_risk(db)
    pos(db, entry=1.0)
    st = st_(25000.0, 17.5, dex_native=0.001, usd=1000.0)                   # -30%: distrusted, under the 40 exit
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    assert st.trusted is False and db.one("SELECT status FROM positions")["status"] == "open"
    db.x("DELETE FROM positions")
    db.set("risk.zincir_gap_exit_pct", 0)
    pos(db, entry=1.0)
    st = st_(25000.0, 11.5, dex_native=0.001, usd=1000.0)
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    assert st.trusted is False and db.one("SELECT status FROM positions WHERE status='open'")


# ---- PumpSwap virtual_quote_reserves (i128 @245) ----
def with_vq(data, vq_raw):
    b = bytearray(data)
    b[245:261] = int(vq_raw).to_bytes(16, "little", signed=True)
    return bytes(b)


def test_pumpswap_virtual_quote_decoded_signed():
    assert onchain.virtual_quote_raw(with_vq(pumpswap_bytes(), 17_580_000_000)) == 17_580_000_000
    assert onchain.virtual_quote_raw(with_vq(pumpswap_bytes(), -2_000_000_000)) == -2_000_000_000
    assert onchain.virtual_quote_raw(b"\0" * 243) == 0                          # old 243-byte layout
    assert onchain.decode_pool(onchain.PUMP_SWAP, with_vq(pumpswap_bytes(), 5), MINT)["vq_raw"] == 5


def test_read_price_uses_effective_quote():
    # live CS sample 2026-11-13: vault 88.925 SOL, vq 17.58 SOL, base 169.16M -> 6.30e-7 (Jupiter 0.01 SOL quote 6.37e-7)
    s = ChainSrc(coin=169_162_305.1, quote=88.925468766)
    async def rpc(method, params, timeout=30.0):
        r = await ChainSrc.rpc(s, method, params, timeout)
        if method == "getAccountInfo":
            r["result"]["value"] = acct(onchain.PUMP_SWAP, with_vq(pumpswap_bytes(base=MINT), 17_580_000_000))
        return r
    got, _ = asyncio.run(read_price(rpc, POOL, MINT, sleep=no_sleep))
    assert abs(got["price_native"] - (88.925468766 + 17.58) / 169_162_305.1) < 1e-15
    assert abs(got["price_native"] / 6.3723e-7 - 1) < 0.015
    assert got["quote_reserve"] == 88.925468766                                  # drain checks stay on the real SOL


def test_watcher_pool_role_updates_virtual_quote():
    w = Watcher("wss://x", None)
    st = PoolState(MINT, POOL, "pumpswap", {CV: "coin", QV: "quote", POOL: "pool"})
    w._apply(st, "coin", tok(1_000_000)); w._apply(st, "quote", sol(10))
    assert abs(st.price_native - 1e-5) < 1e-15
    w._apply(st, "pool", acct(onchain.PUMP_SWAP, with_vq(pumpswap_bytes(), 5_000_000_000)))
    assert abs(st.price_native - 1.5e-5) < 1e-15 and st.quote_reserve == 10
    w._apply(st, "pool", acct(onchain.PUMP_SWAP, with_vq(pumpswap_bytes(), -2_000_000_000)))
    assert abs(st.price_native - 0.8e-5) < 1e-15
