"""6 test_replay (offline part) — the saved CYBRON samples through Scanner → Auditor → Analyst.
The spec's '2 hours of live migrations through PAPER' must be run on the droplet (see report)."""
import asyncio
import json

import config
import fields
from agents.analyst import Analyst
from agents.auditor import Auditor
from agents.scanner import Scanner
from bus import Bus
from conftest import no_sleep
from notifier import Notifier
from sources import ReplaySources

RAW = config.RAW_DIR
MINT = "A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump"
CREATED = 1794428021000
POOL = "DUxG3Y9BAe6kvz2kkXYtHwRpc5jDj2pSh7xk9bYyhwwD"


def agents(db, src, t):
    bus = Bus(db)
    n = Notifier(db, bus)
    kw = dict(clock=lambda: t[0], sleep=no_sleep)
    return bus, Scanner(db, bus, src, n, **kw), Auditor(db, bus, src, n, **kw), Analyst(db, bus, src, n, **kw)


def test_fields_match_mapping():
    m = json.loads((config.ROOT / "fields_mapping.json").read_text())
    mig = fields.migration(json.loads((RAW / "pumpportal_migration.json").read_text()))
    assert mig["mint"] == MINT and mig["pool_label"] == "pump-amm" and mig["creator"] is None
    p = fields.best_pair(json.loads((RAW / "dexscreener_pair.json").read_text()))
    assert p["pair_address"] == m["dexscreener_pair"]["fields"]["pairAddress"]["sample"] == POOL
    r = json.loads((RAW / "rugcheck_report.json").read_text())
    assert fields.rc_lp_locked_pct(r, POOL) == 100
    assert fields.rpc_mint_info(json.loads((RAW / "rpc_getAccountInfo.json").read_text()))["program_owner"] == config.TOKEN_2022_PROGRAM
    assert fields.graph_bundled([]) is None


def test_full_replay(db):
    t = [CREATED / 1000 + 5 * 60]
    src = ReplaySources(graph_mode="empty")
    bus, sc, au, an = agents(db, src, t)
    to_auditor, to_analyst = bus.subscribe("auditor"), bus.subscribe("analyst")
    asyncio.run(sc.add_coin(json.loads((RAW / "pumpportal_migration.json").read_text())))
    c = db.one("SELECT * FROM coins")
    assert c["creator"] == "GAHEsVuqnfv242J96FimQiobCYpZb1suDEALYRHqmPmF" and c["creator_balance_at_migration"] == 0
    asyncio.run(sc.tick())
    assert db.one("SELECT reason FROM scans ORDER BY id DESC")["reason"].startswith("pool")
    db.set("scanner.min_liquidity_usd", 5000)        # the real sample has $6,994 — let it through to test the rest
    t[0] = CREATED / 1000 + 12 * 60
    asyncio.run(sc.tick())
    ev = to_auditor.get_nowait()
    assert ev["pool"] == POOL and ev["top10Source"] == "rugcheck"   # RPC getTokenLargestAccounts was 429 → fallback
    assert db.q("SELECT count(*) n FROM prices")[0]["n"] >= 2
    au.pending[MINT] = ev
    asyncio.run(au.tick())
    assert MINT in au.pending                                       # age 12 < 15: graph not fetched yet
    t[0] = CREATED / 1000 + 16 * 60
    asyncio.run(au.tick())
    a = to_analyst.get_nowait()["auditor"]
    assert a["passed"] and a["layers"][6]["value"] == "None"
    v = asyncio.run(an.process({"mint": MINT, "scanner": ev, "auditor": a}))
    assert v["label"] == "Analyst (rules)" and v["answers"]["flush"] == 0.0 and v["score"] == round(100 * sum([v["answers"]["alive_6h"], v["answers"]["organic"], 0, 1 - v["answers"]["chase"]]) / 4)


def test_kill_reasons_and_copycat(db):
    t = [CREATED / 1000 + 12 * 60]
    _, sc, _, _ = agents(db, ReplaySources(), t)
    db.insert("coins", {"mint": "RUN1", "symbol": "CYBRON", "name": "CYBRON", "max_h24_pct": 500, "updated_at": t[0], "status": "skipped"})
    asyncio.run(sc.add_coin(json.loads((RAW / "pumpportal_migration.json").read_text())))
    db.x("UPDATE coins SET migrated_at=? WHERE mint=?", (t[0], MINT))
    asyncio.run(sc.tick())
    assert db.one("SELECT status_reason FROM coins WHERE mint=?", (MINT,))["status_reason"] == "liq $7.0K"
    db.set("scanner.min_liquidity_usd", 5000)
    db.x("UPDATE coins SET status='waiting' WHERE mint=?", (MINT,)); db.x("DELETE FROM skips")
    asyncio.run(sc.tick())
    assert db.one("SELECT status_reason FROM coins WHERE mint=?", (MINT,))["status_reason"] == "copycat of CYBRON"


def test_graph_error_red_no_data(db):
    t = [CREATED / 1000 + 20 * 60]
    _, _, au, _ = agents(db, ReplaySources(fail_graph=True), t)
    r = asyncio.run(au.audit(MINT, {"pool": POOL, "created_ms": CREATED}))
    assert r["layers"][6]["pass"] is False and r["layers"][6]["value"] == "no data: bundles" and r["layers"][7]["value"] == "not checked"


def test_bundled_network_counts(db):
    t = [CREATED / 1000 + 20 * 60]
    _, _, au, _ = agents(db, ReplaySources(graph_mode="file"), t)
    r = asyncio.run(au.audit(MINT, {"pool": POOL, "created_ms": CREATED}))
    assert r["bundled"] == 4 and r["layers"][6]["pass"]             # 4 < [5]
    db.set("auditor.max_bundled", 4)
    r = asyncio.run(au.audit(MINT, {"pool": POOL, "created_ms": CREATED}))
    assert r["layers"][6]["value"] == "4 bundled wallets" and not r["passed"]
