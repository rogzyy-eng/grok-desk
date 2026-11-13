"""Largest-wallet sell exit (risk.rug_top_holder_sell_pct): lookup at entry, ws watch, exit on every price source."""
import asyncio
import hashlib
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config
import onchain
from agents.risk import rug_early, top_holder_sold
from onchain import PoolState, Watcher, b58decode, b58encode, on_curve, pick_top_holder
from sources import FetchError
from test_onchain import CR, CV, MINT, QV, FakeWS, acct, mk, open_pos, pumpswap_bytes, state


def wallet():
    k = Ed25519PrivateKey.generate().public_key()
    return b58encode(k.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def pda(seed, program=onchain.PUMP_SWAP):
    for bump in range(255, -1, -1):
        h = hashlib.sha256(seed + bytes([bump]) + b58decode(program) + b"ProgramDerivedAddress").digest()
        if not on_curve(b58encode(h)):
            return b58encode(h)


W1, W2 = wallet(), wallet()
POOL_PDA = pda(b"pool")


def parsed(owner, amount):
    return {"data": {"parsed": {"info": {"owner": owner, "tokenAmount": {"uiAmountString": str(amount), "decimals": 6}}},
                     "program": "spl-token"}}


class RPC:
    """Pool CV/QV vaults + a holder list: CV (pool vault), LOCK (PDA owner), TOPATA (wallet W1), DEVATA (W2)."""
    def __init__(self, fail=0, status=429, largest=None):
        self.fail, self.status, self.calls = fail, status, []
        self.largest = largest or [("CVX", 9e8, POOL_PDA), ("LOCK", 5e7, POOL_PDA), ("TOPATA", 4e7, W1), ("DEVATA", 1e7, W2)]
        self.bal = {CV: 1_000_000, QV: 100}

    async def __call__(self, method, params):
        self.calls.append(method)
        if method == "getAccountInfo":
            return {"result": {"value": acct(onchain.PUMP_SWAP, pumpswap_bytes())}}
        if method == "getTokenLargestAccounts":
            if self.fail:
                self.fail -= 1
                raise FetchError("RPC HTTP 429", self.status)
            return {"result": {"context": {}, "value": [{"address": a, "uiAmountString": str(n)} for a, n, _ in self.largest]}}
        if method == "getMultipleAccounts":
            own = {a: (n, o) for a, n, o in self.largest}
            out = []
            for a in params[0]:
                if a in self.bal:
                    out.append(parsed(POOL_PDA, self.bal[a]))
                else:
                    out.append(parsed(own[a][1], own[a][0]))
            return {"result": {"value": out}}
        if method == "getTokenAccountsByOwner":
            return {"result": {"value": []}}
        raise AssertionError(method)


async def nosleep(_s):
    return None


def test_on_curve_wallets_vs_pdas():
    assert on_curve(W1) and on_curve(W2)
    assert not on_curve(POOL_PDA) and not on_curve(onchain.RAY_V4_AUTH) and not on_curve(onchain.RAY_CPMM_AUTH)
    assert not on_curve("bad")


def test_pick_top_holder_skips_pool_programs_and_pdas():
    largest = [{"address": "CVX", "uiAmountString": "900"}, {"address": "AUTH", "uiAmountString": "500"},
               {"address": "LOCK", "uiAmountString": "400"}, {"address": "TOP", "uiAmountString": "40"},
               {"address": "SMALL", "uiAmountString": "1"}]
    owners = {"CVX": W2, "AUTH": onchain.RAY_V4_AUTH, "LOCK": POOL_PDA, "TOP": W1, "SMALL": W2}
    assert pick_top_holder(largest, owners, exclude_accounts=["CVX"]) == ("TOP", W1, 40.0)
    assert pick_top_holder(largest, owners) == ("CVX", W2, 900.0)          # only the vault list excludes CVX
    assert pick_top_holder(largest, owners, exclude_accounts=["CVX"], exclude_owners=[W1]) == ("SMALL", W2, 1.0)
    assert pick_top_holder([], {}) is None


def test_watch_finds_top_holder_and_follows_it_over_ws():
    rpc = RPC()
    sockets = [FakeWS([("TOPATA", parsed(W1, 3e7))])]
    seen = []
    w = None

    async def on_update(mint, st, role):
        seen.append((role, st.top_now))
        w.stopped = True

    async def go():
        nonlocal w
        w = Watcher("wss://x", rpc, on_update, clock=lambda: 50.0, connect=lambda u: sockets[0], sleep=nosleep)
        st = await w.watch(MINT, "POOL")
        got = await w.top_tasks[MINT]
        assert got == ("TOPATA", W1, 4e7) and st.top_start == 4e7 and st.subs["TOPATA"] == "top"
        await w.run()
        return st

    st = asyncio.run(go())
    assert seen == [("top", 3e7)] and st.top_now == 3e7
    subs = [m["params"][0] for m in sockets[0].sent if m["method"] == "accountSubscribe"]
    assert sorted(subs) == sorted([CV, QV, "TOPATA", "POOL"])
    assert rpc.calls.count("getTokenLargestAccounts") == 1                # one lookup at entry, then ws only


def test_creator_as_top_holder_shares_one_subscription():
    w = Watcher("wss://x", None)
    st = state(mint=MINT)
    st.subs["DEVATA"] = "dev"
    w.pools[MINT] = st
    w.rpc = RPC(largest=[("DEVATA", 4e7, W2)])
    got = asyncio.run(w._watch_top(st))
    assert got[0] == "DEVATA" and st.subs["DEVATA"] == "dev+top" and w.by_addr["DEVATA"] == (MINT, "dev+top")
    w._apply(st, "dev+top", parsed(W2, 1e7))
    assert st.dev_now == 1e7 and st.top_now == 1e7


def test_429_backoff_then_give_up_once(caplog):
    slept = []

    async def sleep(s):
        slept.append(s)

    w = Watcher("wss://x", RPC(fail=1), sleep=sleep)
    st = state(mint=MINT); w.pools[MINT] = st
    assert asyncio.run(w._watch_top(st))[0] == "TOPATA" and slept == [5]
    w = Watcher("wss://x", RPC(fail=9), sleep=sleep)
    st = state(mint=MINT); w.pools[MINT] = st
    st2 = state(mint="M2"); w.pools["M2"] = st2
    with caplog.at_level("INFO", logger="risk.onchain"):
        assert asyncio.run(w._watch_top(st)) is None and asyncio.run(w._watch_top(st2)) is None
    assert "429" in w.top_failed[MINT] and st.top_start is None
    assert sum(r.levelname == "WARNING" for r in caplog.records) == 1        # degrade quietly after the first
    w = Watcher("wss://x", RPC(fail=1, status=-32602), sleep=sleep)          # not transient: no retry
    st = state(mint=MINT); w.pools[MINT] = st
    slept.clear()
    assert asyncio.run(w._watch_top(st)) is None and slept == []


def test_unsupported_pool_still_watches_top_holder():
    rpc = RPC()
    rpc_pool = {"owner": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "data": ["AAAA", "base64"]}

    async def call(method, params):
        if method == "getAccountInfo":
            return {"result": {"value": rpc_pool}}
        return await rpc(method, params)

    async def go():
        w = Watcher("wss://x", call, sleep=nosleep)
        assert await w.watch(MINT, "POOL") is None                            # no price from chain
        await w.top_tasks[MINT]
        return w

    w = asyncio.run(go())
    st = w.state(MINT)
    assert "not supported" in w.unsupported[MINT] and st.kind == "holder" and st.price_native is None
    assert st.top_account == "TOPATA" and w.by_addr["TOPATA"] == (MINT, "top")


def test_threshold_and_off_switch():
    c = {"rug_window_s": 20, "rug_liq_drop_fast_pct": 25, "rug_top_holder_sell_pct": 25}
    st = state()
    st.top_start, st.top_now = 100.0, 75.0
    assert top_holder_sold(st, c) is None and rug_early(st, 1000.0, c) is None        # exactly 25%: not more than
    st.top_now = 60.0
    assert rug_early(st, 1000.0, c) == "rug uyarısı: en büyük cüzdan %40 sattı"
    assert rug_early(st, 1000.0, {**c, "rug_top_holder_sell_pct": 0}) is None
    st.top_now = 150.0                                                                 # it bought more: fine
    assert rug_early(st, 1000.0, c) is None


def test_trusted_ws_exit_on_top_holder_sell(db):
    r, n = mk(db)
    open_pos(db)
    st = state()
    st.top_start = st.top_now = 4e7
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    assert db.one("SELECT status FROM positions")["status"] == "open"
    st.top_now = 2e7
    asyncio.run(r.on_ws("M", st, "top"))
    p = db.one("SELECT * FROM positions")
    assert p["status"] == "closed" and p["exit_reason"] == "rug_early" and p["price_source"] == "zincir"
    a = db.one("SELECT * FROM activity WHERE mint='M' AND kind='sold'")
    assert "rug uyarısı: en büyük cüzdan %50 sattı" in a["subtitle"]
    assert any("rug uyarısı: en büyük cüzdan %50 sattı" in s["body"] for s in n.sent if s.get("body"))


def test_untrusted_pool_exits_at_last_dexscreener_price(db):
    r, n = mk(db)
    open_pos(db)
    db.update("positions", "1=1", (), {"last_price": 0.97})
    st = state(dex_native=0.0005, usd=500.0)                   # ws 2x DexScreener -> distrusted
    st.top_start = st.top_now = 4e7
    r.watcher.pools["M"] = st
    asyncio.run(r.on_ws("M", st, "quote"))
    assert st.trusted is False and db.one("SELECT status FROM positions")["status"] == "open"
    st.top_now = 1e7
    asyncio.run(r.on_ws("M", st, "top"))
    p = db.one("SELECT * FROM positions")
    assert p["status"] == "closed" and p["exit_reason"] == "rug_early" and p["exit_price"] == 0.97
    assert p["price_source"] == "DexScreener"


def test_holder_only_state_exits_on_poll(db):
    r, n = mk(db)
    open_pos(db)
    st = PoolState("M", "POOL", "holder", {"TOPATA": "top"})
    st.top_start, st.top_now = 4e7, 2.5e7
    r.watcher.pools["M"] = st
    out = asyncio.run(r.tick())                                 # DexScreener $1.00 + top holder sold 37.5%
    assert out and out[0]["reason"] == "rug_early" and out[0]["detail"] == "rug uyarısı: en büyük cüzdan %38 sattı"
    db.set("risk.rug_top_holder_sell_pct", 0)


def test_setting_default_range_and_installer_keeps_live_value(db):
    assert db.get("risk.rug_top_holder_sell_pct") == 25 and config.RANGES["risk.rug_top_holder_sell_pct"] == (0, 100, 1)
    db.set("risk.rug_top_holder_sell_pct", 40)
    db.seed_settings()                                          # re-run on update: INSERT OR IGNORE
    assert db.get("risk.rug_top_holder_sell_pct") == 40


def test_rpc_429_falls_back_to_rugcheck_and_rereads_balance():
    rpc = RPC(fail=1)
    orig = rpc.__call__

    async def call(method, params):
        if method == "getAccountInfo" and params[0] == "TOPATA":
            return {"result": {"value": parsed(W1, 3.9e7)}}
        return await orig(method, params)

    async def holders(mint):
        return ([{"address": "LPV", "uiAmountString": "9e8"}, {"address": "TOPATA", "uiAmountString": "4e7"}],
                {"LPV": W2, "TOPATA": W1}, {"LPV", "POOLKEY"})

    slept = []

    async def sleep(s):
        slept.append(s)

    w = Watcher("wss://x", call, sleep=sleep, holders=holders)
    st = state(mint=MINT); w.pools[MINT] = st
    assert asyncio.run(w._watch_top(st)) == ("TOPATA", W1, 3.9e7)
    assert st.top_via == "rugcheck" and st.top_start == 3.9e7 and slept == []     # no RPC wait when RugCheck answers


def test_risk_rc_holders_shape(db):
    from test_onchain import mk as mk_risk
    r, _ = mk_risk(db)

    async def rc_report(mint):
        return {"topHolders": [{"address": "A1", "owner": W1, "uiAmount": 5.0}],
                "markets": [{"pubkey": "PK", "liquidityA": "VA", "liquidityB": "VB"}]}

    r.src.rc_report = rc_report
    largest, owners, pools = asyncio.run(r.rc_holders(MINT))
    assert largest == [{"address": "A1", "uiAmountString": "5.0"}] and owners == {"A1": W1} and {"PK", "VA", "VB"} <= pools
