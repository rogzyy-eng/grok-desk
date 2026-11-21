"""2026-11-21 rug guards from the PAPER loss review: pools quoted in another memecoin (Vice Cat / TTWO) are
skipped by the Scanner, and the Auditor reds a coin with fewer than auditor.min_holders holders at layer 8."""
import asyncio

import config
import fields
from agents.scanner import Scanner
from bus import Bus
from conftest import no_sleep
from notifier import Notifier
from onchain import WSOL
from test_audit_freeze import FreezeSrc, run as audit_run

TTWO = "TTWofwAge91oFhZs7kpQdyrVRkmevgM88xijGvQFbKo"


def raw_pair(mint, addr, quote, sym, liq):
    return {"pairAddress": addr, "dexId": "pumpswap", "priceUsd": "0.0002", "liquidity": {"usd": liq},
            "baseToken": {"address": mint, "symbol": "VC", "name": "Vice Cat"},
            "quoteToken": {"address": quote, "symbol": sym}, "marketCap": 100000,
            "volume": {"m5": 3000, "h1": 30000, "h24": 1}, "txns": {"m5": {"buys": 20, "sells": 5}, "h1": {"buys": 200, "sells": 10}},
            "priceChange": {"m5": 1, "h1": 5, "h24": 10}, "pairCreatedAt": 900000}


class NoSrc:
    """Every source call fails -> the scanner stops at 'no data: top 10' after choosing its pool."""
    def __getattr__(self, name):
        async def fail(*a, **k):
            raise RuntimeError("offline")
        return fail


def scanner(db):
    db.set("scanner.min_pool_age_min", 0)
    db.set("scanner.min_liquidity_usd", 10000)
    return Scanner(db, Bus(db), NoSrc(), Notifier(db, Bus(db)), clock=lambda: 1000.0, sleep=no_sleep)


def coin(db, mint):
    db.insert("coins", {"mint": mint, "symbol": "VC", "migrated_at": 990.0, "status": "waiting"})
    return db.one("SELECT * FROM coins WHERE mint=?", (mint,))


def test_defaults():
    assert config.DEFAULTS["scanner"]["sol_quote_only"] is True
    assert config.DEFAULTS["auditor"]["min_holders"] == 100
    assert config.RANGES["auditor.min_holders"] == (0, 1000, 10)


def test_pair_reads_quote_token():
    p = fields.pair(raw_pair("M", "P", TTWO, "TTWO", 36000))
    assert p["quote_mint"] == TTWO and p["quote_symbol"] == "TTWO"


def test_scanner_kills_memecoin_quoted_pool(db):
    s = scanner(db)
    c = coin(db, "VICE")
    assert asyncio.run(s.evaluate(c, [raw_pair("VICE", "POOLT", TTWO, "TTWO", 36000)])) == "kill"
    row = db.one("SELECT status, status_reason FROM coins WHERE mint='VICE'")
    assert row["status"] == "skipped" and row["status_reason"] == "quote TTWO (not SOL/USDC)"


def test_scanner_prefers_sol_pool_over_memecoin_pool(db):
    s = scanner(db)
    c = coin(db, "BOTH")
    pairs = [raw_pair("BOTH", "POOLT", TTWO, "TTWO", 90000), raw_pair("BOTH", "POOLS", WSOL, "SOL", 15000)]
    asyncio.run(s.evaluate(c, pairs))
    row = db.one("SELECT pool, status_reason FROM coins WHERE mint='BOTH'")
    assert row["pool"] == "POOLS" and row["status_reason"] == "no data: top 10"


def test_scanner_guard_off(db):
    s = scanner(db)
    db.set("scanner.sol_quote_only", False)
    c = coin(db, "OFF")
    asyncio.run(s.evaluate(c, [raw_pair("OFF", "POOLT", TTWO, "TTWO", 36000)]))
    assert db.one("SELECT status_reason FROM coins WHERE mint='OFF'")["status_reason"] == "no data: top 10"


def test_auditor_reds_too_few_holders(db):
    assert audit_run(db, FreezeSrc())["passed"]                      # sample has 138 holders >= 100
    db.set("auditor.min_holders", 200)
    r = audit_run(db, FreezeSrc())
    assert not r["passed"] and [l["pass"] for l in r["layers"]] == [True] * 7 + [False]
    assert r["reason"] == "only 138 holders (< 200)" and r["holders"] == 138
    db.set("auditor.min_holders", 0)
    assert audit_run(db, FreezeSrc())["passed"]
