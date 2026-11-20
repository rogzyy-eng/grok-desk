"""2026-11-20 quiet-hours entry filter: no new positions 00-06 Europe/Istanbul (executor + copy), open
positions still managed, wraparound windows (22 -> 6)."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import commands
import config
from agents.executor import in_quiet_hours, quiet_reason
from tests.test_entry_price import ChainSrc, MINT, mk_ex, ref
from tests.test_strategy_reset import risk, zpos

TZ = ZoneInfo("Europe/Istanbul")


def ts(h, m=0, d=20):
    return datetime(2026, 11, d, h, m, tzinfo=TZ).timestamp()


def test_defaults_and_ranges(db):
    ex = config.DEFAULTS["executor"]
    assert ex["quiet_hours_enabled"] is True and ex["quiet_start_h"] == 0 and ex["quiet_end_h"] == 6
    assert config.RANGES["executor.quiet_start_h"] == (0, 23, 1) and config.RANGES["executor.quiet_end_h"] == (0, 23, 1)
    commands.set_setting(db, "executor.quiet_hours_enabled", "on", "test")
    assert db.get("executor.quiet_hours_enabled") is True
    commands.set_setting(db, "executor.quiet_start_h", 22, "test")
    assert db.get("executor.quiet_start_h") == 22


def test_window_plain_and_wraparound():
    assert in_quiet_hours(ts(0), 0, 6) and in_quiet_hours(ts(5, 59), 0, 6)
    assert not in_quiet_hours(ts(6), 0, 6) and not in_quiet_hours(ts(23, 59), 0, 6)
    assert in_quiet_hours(ts(22), 22, 6) and in_quiet_hours(ts(23, 30), 22, 6) and in_quiet_hours(ts(3), 22, 6)
    assert not in_quiet_hours(ts(6), 22, 6) and not in_quiet_hours(ts(12), 22, 6) and not in_quiet_hours(ts(21, 59), 22, 6)
    assert not in_quiet_hours(ts(3), 6, 6)                                         # start == end: never quiet


def test_quiet_reason_respects_switch(db):
    db.set("executor.quiet_hours_enabled", True)
    assert quiet_reason(db, ts(2)) == "gece saati: giriş yok (00:00–06:00)"
    assert quiet_reason(db, ts(14)) is None
    db.set("executor.quiet_hours_enabled", False)
    assert quiet_reason(db, ts(2)) is None


def mk_clock_ex(db, now):
    ex = mk_ex(db, ChainSrc())
    ex.clock = lambda: now
    return ex


def test_no_entry_at_night_entry_by_day(db):
    db.set("executor.quiet_hours_enabled", True)
    ex = mk_clock_ex(db, ts(3))
    assert asyncio.run(ex.process({"mint": MINT, "pair": ref()})) is None
    assert not db.open_positions()
    assert db.one("SELECT reason FROM decisions WHERE agent='executor' AND decision='skip'")["reason"].startswith("gece saati: giriş yok")
    ex.clock = lambda: ts(14)
    assert asyncio.run(ex.process({"mint": MINT, "pair": ref()}))
    assert len(db.open_positions()) == 1


def test_copy_entry_blocked_at_night(db):
    db.set("executor.quiet_hours_enabled", True)
    ex = mk_clock_ex(db, ts(1))
    assert asyncio.run(ex.open_copy({"mint": MINT, "pair": ref(), "handle": "h", "wallet": "w", "sig": "s"})) is None
    assert db.one("SELECT reason FROM decisions WHERE agent='executor' AND decision='skip'")["reason"].startswith("gece saati")


def test_open_position_still_exits_at_night(db):
    db.set("executor.quiet_hours_enabled", True)
    zpos(db)
    out = asyncio.run(risk(db, 1.0, t=ts(3)).tick({"Z": 1.101 * 0.90}))       # -10% at 03:00: stop still fires
    assert out and out[0]["reason"] == "stop"
    assert db.one("SELECT status FROM positions")["status"] == "closed"
