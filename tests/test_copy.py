"""PAPER copy agent: watch configured wallets, Auditor then Executor, separate max_open,
follow-sell exit. Never places a FOMO order."""
import asyncio
import inspect

import config
from agents.copy import Copy, fmt_quote, parse_wallets
from agents.executor import Executor, open_count
from agents.risk import Risk
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier
from onchain import USDC, WSOL, wallet_flows

LEO = "3qBVJLMAWXCbW3nhz99T99in82zZeUHkbDeSDJThbN4Z"
MINT = "CopyToken111111111111111111111111111111111"
SIG = "5" * 64
# unverified second leo address from fomo-api.com — must not be in the seed
LEO_EXTRA = "9LF3d3fxAbeabsxn2GLzMKqNbS1MUMtKvMcNLjTPbDNo"
BRON = "2EJPiGHWCddwUnoWoRHD3dfycjQwDV9iQceshn2HDpht"
DMAN = "AY2pvoJqBDw4QtARhfHLE5Fytdtj8HGoQHtYG5AFYrHG"


def tx_swap(wallet=LEO, mint=MINT, sol=1.2, usdc=None, token_in=0.0, token_out=0.0,
            token_pre=0.0, fee=5000, block_time=None, sig=SIG, failed=False):
    """jsonParsed getTransaction: wallet is the fee payer (index 0). sol is SOL spent (not including fee)."""
    keys = [{"pubkey": wallet}, {"pubkey": "PoolCopy11111111111111111111111111111111"},
            {"pubkey": "AtaQ11111111111111111111111111111111111111"},
            {"pubkey": "AtaT11111111111111111111111111111111111111"}]
    fee_l = fee
    start = 5_000_000_000
    if usdc is None:
        spent = int(sol * 1_000_000_000)
        pre_b = [start, 0, 0, 0]
        post_b = [start - spent - fee_l, 0, 0, 0]
    else:
        pre_b = [start, 0, 0, 0]
        post_b = [start - fee_l, 0, 0, 0]
    pre_tb, post_tb = [], []
    if usdc:
        pre_tb.append({"accountIndex": 2, "mint": USDC, "owner": wallet,
                       "uiTokenAmount": {"uiAmount": float(usdc), "decimals": 6}})
        post_tb.append({"accountIndex": 2, "mint": USDC, "owner": wallet,
                        "uiTokenAmount": {"uiAmount": 0.0, "decimals": 6}})
    pre_amt = float(token_pre)
    post_amt = pre_amt + float(token_in) - float(token_out)
    pre_tb.append({"accountIndex": 3, "mint": mint, "owner": wallet,
                   "uiTokenAmount": {"uiAmount": pre_amt, "decimals": 6}})
    post_tb.append({"accountIndex": 3, "mint": mint, "owner": wallet,
                    "uiTokenAmount": {"uiAmount": post_amt, "decimals": 6}})
    return {"blockTime": block_time, "signature": sig,
            "transaction": {"signatures": [sig], "message": {"accountKeys": keys}},
            "meta": {"err": {"InstructionError": [0, "Custom"]} if failed else None, "fee": fee_l,
                     "preBalances": pre_b, "postBalances": post_b,
                     "preTokenBalances": pre_tb, "postTokenBalances": post_tb}}


class StubAuditor:
    def __init__(self, passed=True, reason="dev holds 8%"):
        self.passed, self.reason, self.calls = passed, reason, []

    def rejected_tonight(self, mint):
        return False

    async def audit(self, mint, scan, now=None):
        self.calls.append(mint)
        return {"passed": self.passed, "reason": None if self.passed else self.reason, "layers": []}


def mk(db, auditor=None, src=None, t=1_700_000_000, sol_usd=100.0):
    bus = Bus(db)
    n = Notifier(db, bus)
    src = src or FakeSrc(price=1.0, liq=50_000)
    ex = Executor(db, bus, src, n, clock=lambda: t, sleep=no_sleep)
    rk = Risk(db, bus, src, n, clock=lambda: t, sleep=no_sleep)
    au = auditor or StubAuditor(True)
    cp = Copy(db, bus, src, n, clock=lambda: t, sleep=no_sleep, auditor=au, executor=ex, risk=rk)
    cp.sol_usd = sol_usd
    return cp, ex, rk, au, n


# ---- settings ----
def test_copy_settings_seeded_and_insert_or_ignore(db):
    wallets = parse_wallets(db.get("copy.wallets"))
    assert db.get("copy.enabled") is True
    assert db.get("copy.min_buy_usd") == 100
    assert db.get("copy.max_open") == 3
    assert db.get("copy.max_age_s") == 60
    assert db.get("copy.follow_sell_pct") == 50
    assert len(wallets) == 8
    handles = [w["handle"] for w in wallets]
    addrs = [w["address"] for w in wallets]
    assert handles == ["joecroskey", "end837", "g41t3r98", "leo", "0xbr9", "sol589", "oledsed", "horseimnot"]
    assert "bronbroncore" not in handles and "dman_sol" not in handles
    assert LEO_EXTRA not in addrs and BRON not in addrs and DMAN not in addrs
    assert wallets[3] == {"handle": "leo", "address": LEO}
    assert config.RANGES["copy.min_buy_usd"] == (10, 10000, 10)
    assert config.RANGES["copy.max_open"] == (1, 10, 1)
    assert config.RANGES["copy.max_age_s"] == (5, 600, 5)
    assert config.RANGES["copy.follow_sell_pct"] == (0, 100, 1)
    assert "copy" in config.AGENTS
    db.set("copy.min_buy_usd", 250)
    db.set("copy.wallets", [{"handle": "only", "address": LEO}])
    db.seed_settings()
    assert db.get("copy.min_buy_usd") == 250
    assert parse_wallets(db.get("copy.wallets")) == [{"handle": "only", "address": LEO}]


def test_parse_wallets_and_fmt():
    text = "@leo 3qBVJLMAWXCbW3nhz99T99in82zZeUHkbDeSDJThbN4Z\nend837 5DBkonys2H4qHr5x9CCeXPLSWEFbnnLFP1RdfxjJgzvy"
    got = parse_wallets(text)
    assert got[0]["handle"] == "leo" and got[0]["address"] == LEO
    assert got[1]["handle"] == "end837"
    assert parse_wallets([LEO])[0]["address"] == LEO
    assert fmt_quote("SOL", 1.2) == "1.2 SOL"
    assert fmt_quote("USDC", 150) == "150 USDC"


def test_wallet_flows_sol_and_usdc_buy_and_sell():
    buy = wallet_flows(tx_swap(sol=1.2, token_in=1000, token_pre=0, block_time=10), LEO)
    assert buy["buys"] and not buy["sells"]
    b = buy["buys"][0]
    assert b["mint"] == MINT and b["quote"] == "SOL" and abs(b["quote_amount"] - 1.2) < 1e-9
    assert b["token_post"] == 1000
    usdc = wallet_flows(tx_swap(sol=0, usdc=150, token_in=50, token_pre=0), LEO)
    assert usdc["buys"][0]["quote"] == "USDC" and usdc["buys"][0]["quote_amount"] == 150
    sell = wallet_flows(tx_swap(sol=0, token_in=0, token_out=600, token_pre=1000), LEO)
    assert not sell["buys"] and sell["sells"][0]["token_post"] == 400
    assert wallet_flows(tx_swap(sol=1.2, token_in=10, failed=True), LEO) is None


# ---- agent path ----
def test_copy_buy_auditor_pass_opens_paper(db):
    t = 1_700_000_000
    cp, ex, rk, au, n = mk(db, t=t)
    tx = tx_swap(sol=1.2, token_in=1000, block_time=t - 5)
    asyncio.run(cp.on_tx(LEO, SIG, tx))
    assert au.calls == [MINT]
    pos = db.open_positions()
    assert len(pos) == 1
    p = pos[0]
    assert p["source"] == "copy" and p["copy_handle"] == "leo" and p["copy_wallet"] == LEO
    assert p["copy_sig"] == SIG and p["copy_start_qty"] == 1000
    assert p["status"] == "open" and p["mode"] == "PAPER"
    act = db.one("SELECT * FROM activity WHERE agent='copy' ORDER BY id DESC")
    assert act["title"] == "kopya: @leo 1.2 SOL aldı → denetimden geçti, PAPER aldım"
    assert act["kind"] == "bought"
    # never a FOMO order
    src = inspect.getsource(Copy) + inspect.getsource(ex.open_copy)
    for bad in ("sendTransaction", "private_key", "password", "swap("):
        assert bad not in src


def test_copy_buy_auditor_reject(db):
    t = 1_700_000_000
    au = StubAuditor(False, "dev holds 8%")
    cp, *_ = mk(db, auditor=au, t=t)
    asyncio.run(cp.on_tx(LEO, SIG, tx_swap(sol=1.2, token_in=1000, block_time=t - 2)))
    assert not db.open_positions()
    act = db.one("SELECT * FROM activity WHERE agent='copy' ORDER BY id DESC")
    assert act["title"] == "kopya: @leo 1.2 SOL aldı → denetim reddetti"
    assert act["kind"] == "rejected"
    assert "dev holds 8%" in act["subtitle"]


def test_copy_skips_old_and_small_and_live_and_disabled(db):
    t = 1_700_000_000
    cp, *_ = mk(db, t=t)
    asyncio.run(cp.on_tx(LEO, SIG, tx_swap(sol=1.2, token_in=10, block_time=t - 120)))  # 120s > 60
    assert not db.open_positions()
    reason = db.one("SELECT reason FROM decisions WHERE agent='copy' ORDER BY id DESC")["reason"]
    assert "sinyal 120s eski" in reason

    db.x("DELETE FROM decisions")
    asyncio.run(cp.on_tx(LEO, "A" * 64, tx_swap(sol=0, usdc=40, token_in=10, block_time=t - 1, sig="A" * 64)))
    reason = db.one("SELECT reason FROM decisions WHERE agent='copy' ORDER BY id DESC")["reason"]
    assert "min alış" in reason and not db.open_positions()

    db.x("DELETE FROM decisions")
    db.set("desk.mode", "LIVE")
    db.set("desk.live_stack", 1000)
    asyncio.run(cp.on_tx(LEO, "B" * 64, tx_swap(sol=1.2, token_in=10, block_time=t - 1, sig="B" * 64)))
    assert not db.open_positions()
    assert "sadece PAPER" in db.one("SELECT reason FROM decisions WHERE agent='copy' ORDER BY id DESC")["reason"]

    db.set("desk.mode", "PAPER")
    db.set("copy.enabled", False)
    db.x("DELETE FROM decisions")
    asyncio.run(cp.on_tx(LEO, "C" * 64, tx_swap(sol=1.2, token_in=10, block_time=t - 1, sig="C" * 64)))
    assert not db.q("SELECT 1 FROM decisions WHERE agent='copy'") and not db.open_positions()


def test_copy_max_open_separate_from_executor(db):
    t = 1_700_000_000
    cp, ex, *_ = mk(db, t=t)
    # three copy positions fill copy.max_open=3
    for i in range(3):
        mint = f"CopyTok{i}" + "1" * (32 - len(str(i)))
        sig = chr(65 + i) * 64
        db.insert("coins", {"mint": mint, "symbol": f"C{i}"})
        asyncio.run(cp.on_tx(LEO, sig, tx_swap(mint=mint, sol=1.2, token_in=100, block_time=t - 1, sig=sig)))
    assert open_count(db, "copy") == 3 and open_count(db) == 0
    # fourth copy is refused
    mint = "CopyTokX" + "1" * 24
    asyncio.run(cp.on_tx(LEO, "X" * 64, tx_swap(mint=mint, sol=1.2, token_in=100, block_time=t - 1, sig="X" * 64)))
    assert open_count(db, "copy") == 3
    assert "kopya limiti doldu" in db.one("SELECT reason FROM decisions WHERE mint=? ORDER BY id DESC", (mint,))["reason"]
    # executor book is still empty, so a scanner-path buy can open
    db.insert("coins", {"mint": "SCAN1", "symbol": "SC"})
    assert asyncio.run(ex.process({"mint": "SCAN1", "pair": {"price_usd": 1.0, "liq_usd": 50_000}}))
    assert open_count(db) == 1 and open_count(db, "copy") == 3
    # fill executor.max_open (default 2) with a second scanner buy
    db.insert("coins", {"mint": "SCAN2", "symbol": "SD"})
    assert asyncio.run(ex.process({"mint": "SCAN2", "pair": {"price_usd": 1.0, "liq_usd": 50_000}}))
    db.insert("coins", {"mint": "SCAN3", "symbol": "SE"})
    assert asyncio.run(ex.process({"mint": "SCAN3", "pair": {"price_usd": 1.0, "liq_usd": 50_000}})) is None
    assert open_count(db) == 2 and open_count(db, "copy") == 3


def test_copy_keeps_reentry_cooldown(db):
    t = 1_700_000_000
    db.insert("coins", {"mint": MINT, "symbol": "TST"})
    db.insert("positions", {"mint": MINT, "status": "closed", "pnl_usd": -2, "closed_at": t - 60,
                            "entry_price": 1, "size_usd": 10, "qty": 0, "opened_at": t - 120, "mode": "PAPER"})
    cp, *_ = mk(db, t=t)
    asyncio.run(cp.on_tx(LEO, SIG, tx_swap(sol=1.2, token_in=10, block_time=t - 1)))
    assert not db.q("SELECT 1 FROM positions WHERE status='open'")
    assert "tekrar giriş beklemesi" in db.one("SELECT reason FROM decisions WHERE agent='copy' ORDER BY id DESC")["reason"]


def test_follow_sell_exits_at_threshold(db):
    t = 1_700_000_000
    cp, ex, rk, au, n = mk(db, t=t)
    asyncio.run(cp.on_tx(LEO, SIG, tx_swap(sol=1.2, token_in=1000, token_pre=0, block_time=t - 1)))
    pos = db.open_positions()[0]
    assert pos["copy_start_qty"] == 1000
    # sell 20% — stay in
    asyncio.run(cp.on_tx(LEO, "S" * 64, tx_swap(sol=0, token_out=200, token_pre=1000, block_time=t - 1, sig="S" * 64)))
    assert db.open_positions()[0]["id"] == pos["id"]
    # sell down to 400 (60% of start) — exit
    asyncio.run(cp.on_tx(LEO, "T" * 64, tx_swap(sol=0, token_out=400, token_pre=800, block_time=t - 1, sig="T" * 64)))
    p = db.one("SELECT * FROM positions WHERE id=?", (pos["id"],))
    assert p["status"] == "closed" and p["exit_reason"] == "copy_sell"
    act = db.one("SELECT * FROM activity WHERE kind='sold' ORDER BY id DESC")
    assert "kopya: @leo %60 sattı → çıktım" in act["title"]


def test_copy_agent_in_health_and_settings(db, monkeypatch):
    monkeypatch.setattr(config, "APP_TOKEN", "T" * 48)
    from fastapi.testclient import TestClient
    from api import make_app
    from main import Desk
    desk = Desk(db=db, src=FakeSrc())
    assert desk.copy.name == "copy" and desk.copy in desk.agents
    c = TestClient(make_app(desk))
    h = {"Authorization": "Bearer " + "T" * 48}
    health = c.get("/api/health", headers=h).json()
    agents = c.get("/api/agents", headers=h).json()["agents"]
    names = [a["name"] for a in agents]
    assert names == list(config.AGENTS) and "copy" in names
    # heartbeat from Copy.__init__
    assert "copy" in health["agents"]
    s = c.get("/api/settings", headers=h).json()
    assert s["settings"]["copy"]["enabled"] is True
    assert s["settings"]["copy"]["min_buy_usd"] == 100
    assert s["settings"]["copy"]["max_open"] == 3
    assert s["ranges"]["copy.follow_sell_pct"] == [0, 100, 1]
    wallets = s["settings"]["copy"]["wallets"]
    assert len(wallets) == 8 and wallets[3]["handle"] == "leo"
    # setting write accepts the JSON list
    r = c.post("/api/settings", json={"key": "copy.min_buy_usd", "value": 80}, headers=h)
    assert r.status_code == 200 and db.get("copy.min_buy_usd") == 80


def test_copy_py_does_not_hardcode_wallets():
    import agents.copy as mod
    src = inspect.getsource(mod)
    for addr in (LEO, LEO_EXTRA, BRON, DMAN,
                 "8bXzTJq3sNkzD8T1mXVCaMvg1D3CL1heYJBgSPM8WVow",
                 "5DBkonys2H4qHr5x9CCeXPLSWEFbnnLFP1RdfxjJgzvy"):
        assert addr not in src
