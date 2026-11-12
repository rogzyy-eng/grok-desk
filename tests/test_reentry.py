"""Re-entry cooldown: skip a buy after a losing close on the same mint or the same
symbol (case-insensitive, trimmed), and while that mint/symbol is already open."""
import asyncio

import config
from agents.executor import Executor, reentry_skip_reason
from bus import Bus
from conftest import FakeSrc, no_sleep
from notifier import Notifier


def _closed(db, mint, symbol, closed_at, pnl, status="closed"):
    db.x("INSERT OR REPLACE INTO coins(mint, symbol) VALUES(?,?)", (mint, symbol))
    db.insert("positions", {
        "mint": mint, "opened_at": closed_at - 60, "entry_price": 1.0, "size_usd": 10,
        "status": status, "closed_at": closed_at, "pnl_usd": pnl, "mode": "PAPER",
        "qty": 0, "stop_pct": 15, "peak_price": 1.0,
    })


def _ex(db, clock, src=None):
    n = Notifier(db, Bus(db))
    return Executor(db, Bus(db), src or FakeSrc(price=1.0, liq=50000), n, clock=clock, sleep=no_sleep)


def _buy(ex, mint, symbol, db):
    if not db.one("SELECT 1 FROM coins WHERE mint=?", (mint,)):
        db.insert("coins", {"mint": mint, "symbol": symbol})
    else:
        db.x("UPDATE coins SET symbol=? WHERE mint=?", (symbol, mint))
    return asyncio.run(ex.process({"mint": mint, "pair": {"price_usd": 1.0, "liq_usd": 50000}}))


def test_default_and_range(db):
    assert db.get("executor.reentry_cooldown_min") == 60
    assert config.DEFAULTS["executor"]["reentry_cooldown_min"] == 60
    assert config.RANGES["executor.reentry_cooldown_min"] == (0, 1440, 5)


def test_seed_adds_key_only_if_missing(db):
    db.set("executor.reentry_cooldown_min", 15)
    db.seed_settings()
    assert db.get("executor.reentry_cooldown_min") == 15
    db.x("DELETE FROM settings WHERE key=?", ("executor.reentry_cooldown_min",))
    assert db.get("executor.reentry_cooldown_min") is None
    db.seed_settings()
    assert db.get("executor.reentry_cooldown_min") == 60


def test_same_mint_loss_within_cooldown_skips(db):
    now = 10_000.0
    _closed(db, "MINT1", "ALPHA", now - 6 * 60, -2.5)
    why = reentry_skip_reason(db, "MINT1", now, 60)
    assert why == "tekrar giriş beklemesi: ALPHA 6 dk önce zararla kapandı"
    ex = _ex(db, lambda: now)
    assert _buy(ex, "MINT1", "ALPHA", db) is None
    assert db.one("SELECT status_reason FROM coins WHERE mint='MINT1'")["status_reason"] == why
    assert db.one("SELECT decision, reason FROM decisions WHERE agent='executor' AND mint='MINT1' ORDER BY id DESC")["decision"] == "skip"


def test_same_symbol_different_mint_and_case(db):
    """Copycats reuse names: marsoperator and MARSOPERATOR were different mints."""
    now = 10_000.0
    _closed(db, "mint_old", "marsoperator", now - 6 * 60, -3.0)
    db.insert("coins", {"mint": "mint_new", "symbol": "MARSOPERATOR"})
    why = reentry_skip_reason(db, "mint_new", now, 60)
    assert why == "tekrar giriş beklemesi: MARSOPERATOR 6 dk önce zararla kapandı"
    ex = _ex(db, lambda: now)
    assert _buy(ex, "mint_new", "MARSOPERATOR", db) is None
    assert not db.open_positions()


def test_symbol_trim_and_casefold(db):
    now = 10_000.0
    _closed(db, "A", "  FooBar  ", now - 12 * 60, -1.0)
    db.insert("coins", {"mint": "B", "symbol": "foobar"})
    why = reentry_skip_reason(db, "B", now, 60)
    assert why == "tekrar giriş beklemesi: foobar 12 dk önce zararla kapandı"


def test_after_cooldown_allows_rebuy(db):
    now = 10_000.0
    _closed(db, "M1", "ZZ", now - 60 * 60, -2.0)   # exactly 60 min ago: window is strict '>'
    assert reentry_skip_reason(db, "M1", now, 60) is None
    ex = _ex(db, lambda: now)
    assert _buy(ex, "M1", "ZZ", db) is not None
    assert len(db.open_positions()) == 1


def test_just_inside_cooldown_still_skips(db):
    now = 10_000.0
    _closed(db, "M1", "ZZ", now - 60 * 60 + 1, -2.0)
    why = reentry_skip_reason(db, "M1", now, 60)
    assert "59 dk önce zararla kapandı" in why


def test_profit_close_does_not_block(db):
    now = 10_000.0
    _closed(db, "M1", "WIN", now - 5 * 60, 4.0)
    assert reentry_skip_reason(db, "M1", now, 60) is None
    ex = _ex(db, lambda: now)
    assert _buy(ex, "M2", "WIN", db) is not None


def test_zero_cooldown_allows_loss_rebuy(db):
    db.set("executor.reentry_cooldown_min", 0)
    now = 10_000.0
    _closed(db, "M1", "LOST", now - 30, -2.0)
    assert reentry_skip_reason(db, "M1", now, 0) is None
    ex = _ex(db, lambda: now)
    assert _buy(ex, "M1", "LOST", db) is not None


def test_open_same_symbol_skipped_even_if_cooldown_off(db):
    db.set("executor.reentry_cooldown_min", 0)
    now = 10_000.0
    db.insert("coins", {"mint": "OPEN1", "symbol": "marsoperator"})
    db.insert("positions", {"mint": "OPEN1", "opened_at": now - 10, "entry_price": 1.0,
                            "size_usd": 10, "status": "open", "qty": 10, "mode": "PAPER",
                            "stop_pct": 15, "peak_price": 1.0, "stop_price": 0.85})
    db.insert("coins", {"mint": "COPY", "symbol": "  MARSOPERATOR  "})
    why = reentry_skip_reason(db, "COPY", now, 0)
    assert why == "tekrar giriş beklemesi: MARSOPERATOR zaten açık"
    ex = _ex(db, lambda: now)
    assert _buy(ex, "COPY", "  MARSOPERATOR  ", db) is None
    assert len(db.open_positions()) == 1
    assert db.open_positions()[0]["mint"] == "OPEN1"


def test_already_holding_same_mint(db):
    now = 10_000.0
    db.insert("coins", {"mint": "H1", "symbol": "HOLD"})
    db.insert("positions", {"mint": "H1", "opened_at": now - 10, "entry_price": 1.0,
                            "size_usd": 10, "status": "open", "qty": 10, "mode": "PAPER",
                            "stop_pct": 15, "peak_price": 1.0, "stop_price": 0.85})
    assert reentry_skip_reason(db, "H1", now, 60) == "already holding"


def test_other_symbol_not_blocked(db):
    now = 10_000.0
    _closed(db, "M1", "DOG", now - 5 * 60, -2.0)
    db.insert("coins", {"mint": "M2", "symbol": "CAT"})
    assert reentry_skip_reason(db, "M2", now, 60) is None


def test_applies_in_paper_and_live(db):
    now = 10_000.0
    _closed(db, "old", "SAME", now - 3 * 60, -1.2)
    # PAPER
    ex = _ex(db, lambda: now)
    assert _buy(ex, "newP", "same", db) is None
    assert db.get("desk.mode") == "PAPER"
    assert not db.open_positions()
    # LIVE
    db.set("desk.mode", "LIVE")
    db.x("INSERT OR REPLACE INTO settings VALUES('desk.live_stack','200',0)")
    ex = _ex(db, lambda: now)
    assert _buy(ex, "newL", "Same", db) is None
    assert not db.open_positions()
    paper = db.one("SELECT reason FROM decisions WHERE mint='newP' AND decision='skip'")["reason"]
    live = db.one("SELECT reason FROM decisions WHERE mint='newL' AND decision='skip'")["reason"]
    assert paper.startswith("tekrar giriş beklemesi: same")
    assert live.startswith("tekrar giriş beklemesi: Same")
    assert "3 dk önce zararla kapandı" in paper and "3 dk önce zararla kapandı" in live
