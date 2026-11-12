"""db.py — SQLite in WAL mode. STEP 4 columns first; extra columns (after the spec ones) are
noted with -- extra and exist only so the desk can run (status, qty, etc.)."""
import json
import sqlite3
import threading
import time
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins(mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, pool TEXT, migrated_at REAL,
  creator TEXT, creator_balance_at_migration REAL, holders INTEGER,
  chain TEXT DEFAULT 'solana', signature TEXT, pool_label TEXT, status TEXT DEFAULT 'waiting',
  status_reason TEXT, max_h24_pct REAL, updated_at REAL);                         -- extra: chain..updated_at
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, mint TEXT, ts REAL, liquidity_usd REAL, top10_pct REAL,
  h1_change_pct REAL, age_min REAL, verdict TEXT, reason TEXT,
  price REAL, mcap REAL, volume_m5 REAL, volume_h1 REAL, volume_h24 REAL, buys_m5 REAL, sells_m5 REAL,
  buys_h1 REAL, sells_h1 REAL, m5_change_pct REAL, h24_change_pct REAL, pool TEXT, created_ms REAL);
CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY, mint TEXT, ts REAL, layer INTEGER, name TEXT,
  value_text TEXT, pass INTEGER, source TEXT, run_id INTEGER, ms INTEGER);          -- pass NULL = not checked
CREATE TABLE IF NOT EXISTS verdicts(id INTEGER PRIMARY KEY, mint TEXT, ts REAL, alive_6h REAL, organic REAL,
  flush REAL, chase REAL, score INTEGER, pass INTEGER, reason TEXT, input_tokens INTEGER, mode TEXT, model TEXT);
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY, mint TEXT, ts REAL, price REAL, size_usd REAL,
  stack_pct REAL, link TEXT, mode TEXT, status TEXT, side TEXT DEFAULT 'buy', reason TEXT, pushed_at REAL);
CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, mint TEXT, opened_at REAL, entry_price REAL,
  size_usd REAL, peak_price REAL, stop_price REAL, trail_active INTEGER DEFAULT 0, closed_at REAL,
  exit_price REAL, exit_reason TEXT, pnl_usd REAL, fees_usd REAL, mode TEXT,
  status TEXT, qty REAL, stop_pct REAL, entry_fee REAL, exit_line REAL, exit_requested_at REAL,
  repushed INTEGER DEFAULT 0, no_price_polls INTEGER DEFAULT 0, last_price REAL, signal_id INTEGER);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, mint TEXT, agent TEXT,
  title TEXT, subtitle TEXT, value TEXT);                                          -- kind bought|sold|stopped|rejected
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, mint TEXT, decision TEXT,
  reason TEXT, data TEXT);                                                         -- extra: /api/agents/{name}/decisions
CREATE TABLE IF NOT EXISTS prices(mint TEXT, ts REAL, price REAL, volume_m5 REAL, liquidity_usd REAL);
CREATE TABLE IF NOT EXISTS balance(ts REAL, value REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS change_log(ts REAL, key TEXT, old TEXT, new TEXT, who TEXT);
CREATE TABLE IF NOT EXISTS skips(mint TEXT, until REAL, agent TEXT, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS heartbeats(agent TEXT PRIMARY KEY, ts REAL, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS push_subs(endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS ledger(ts REAL, mode TEXT, cash REAL, delta REAL, reason TEXT);  -- extra: cash moves
CREATE INDEX IF NOT EXISTS ix_prices ON prices(mint, ts);
CREATE INDEX IF NOT EXISTS ix_scans ON scans(mint, ts);
CREATE INDEX IF NOT EXISTS ix_activity ON activity(ts);
CREATE INDEX IF NOT EXISTS ix_audits ON audits(mint, run_id);
"""


class DB:
    def __init__(self, path=None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cx = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.cx.row_factory = sqlite3.Row
        self.cx.execute("PRAGMA journal_mode=WAL")
        self.cx.execute("PRAGMA synchronous=NORMAL")
        self.cx.execute("PRAGMA busy_timeout=5000")
        self.cx.executescript(SCHEMA)
        self.migrate()
        self.seed_settings()

    # extra columns added after the first install; ALTER only when missing (keeps existing DBs + open positions)
    # 2026-11-12: entry liquidity, per-position risk snapshot, paper price impact, runner partial close
    # 2026-11-13: take-profit snapshot (tp_pct, tp_sell_frac)
    MIGRATIONS = {"positions": {"volatile": "INTEGER DEFAULT 0", "m5_change_pct": "REAL",
                                "entry_liq_usd": "REAL", "trail_start_pct": "REAL", "trail_pct": "REAL", "timeout_h": "REAL",
                                "runner_trail_pct": "REAL", "entry_impact_pct": "REAL", "market_entry_price": "REAL",
                                "runner": "INTEGER DEFAULT 0", "partial_at": "REAL", "partial_qty": "REAL",
                                "partial_price": "REAL", "partial_usd": "REAL", "partial_fee": "REAL", "last_liq_usd": "REAL",
                                "price_source": "TEXT", "tp_pct": "REAL", "tp_sell_frac": "REAL"}}

    def migrate(self):
        for table, cols in self.MIGRATIONS.items():
            have = {r[1] for r in self.cx.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in cols.items():
                if col not in have:
                    self.cx.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def x(self, sql, args=()):
        with self.lock:
            return self.cx.execute(sql, args).lastrowid

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.cx.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def journal_mode(self):
        return self.cx.execute("PRAGMA journal_mode").fetchone()[0]

    def insert(self, table, row: dict):
        return self.x(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))

    def update(self, table, where, wargs, row: dict):
        self.x(f"UPDATE {table} SET {','.join(f'{k}=?' for k in row)} WHERE {where}", tuple(row.values()) + tuple(wargs))

    # ---- settings ----
    def seed_settings(self):
        now = time.time()
        for sec, vals in config.DEFAULTS.items():
            for k, v in vals.items():
                self.x("INSERT OR IGNORE INTO settings VALUES(?,?,?)", (f"{sec}.{k}", json.dumps(v), now))
        for k in config.OBSOLETE:
            self.x("DELETE FROM settings WHERE key=?", (k,))
        for k, v in {"desk.paper_nights": 0, "desk.paper_since": None, "desk.paused_agents": [],
                     "desk.layer8_proof": None, "desk.last_night_closed": None, "desk.last_page_url": None,
                     "desk.layer8_down_kills": 0}.items():
            self.x("INSERT OR IGNORE INTO settings VALUES(?,?,?)", (k, json.dumps(v), now))
        if not self.q("SELECT 1 FROM ledger LIMIT 1"):
            s = config.DEFAULTS["desk"]["paper_start_stack"]
            self.insert("ledger", {"ts": now, "mode": "PAPER", "cash": s, "delta": 0, "reason": "paper start stack"})
            self.insert("balance", {"ts": now, "value": s})

    def get(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return default if r is None else json.loads(r["value"])

    def section(self, sec):
        return {r["key"].split(".", 1)[1]: json.loads(r["value"])
                for r in self.q("SELECT key,value FROM settings WHERE key LIKE ?", (sec + ".%",))}

    def all_settings(self):
        out = {}
        for r in self.q("SELECT key,value FROM settings ORDER BY key"):
            s, k = r["key"].split(".", 1)
            out.setdefault(s, {})[k] = json.loads(r["value"])
        return out

    def set(self, key, value, who="system"):
        old = self.get(key)
        if old == value:
            return False
        now = time.time()
        self.x("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
               "updated_at=excluded.updated_at", (key, json.dumps(value), now))
        self.insert("change_log", {"ts": now, "key": key, "old": json.dumps(old), "new": json.dumps(value), "who": who})
        return True

    # ---- money ----
    def cash(self):
        r = self.one("SELECT cash FROM ledger ORDER BY rowid DESC LIMIT 1")
        return float(r["cash"]) if r else 0.0

    def move_cash(self, delta, reason, mode=None):
        new = round(self.cash() + delta, 6)
        self.insert("ledger", {"ts": time.time(), "mode": mode or self.get("desk.mode"), "cash": new,
                               "delta": round(delta, 6), "reason": reason})
        return new

    def equity(self):
        val = 0.0
        for p in self.open_positions():
            if p["status"] == "open":
                lp = p["last_price"] or p["entry_price"]
                val += (p["qty"] or 0) * lp
        return round(self.cash() + val, 6)

    def snapshot_balance(self, ts=None):
        self.insert("balance", {"ts": ts or time.time(), "value": self.equity()})

    # ---- misc ----
    def heartbeat(self, agent, status="ok", note=""):
        self.x("INSERT INTO heartbeats VALUES(?,?,?,?) ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts,"
               "status=excluded.status, note=excluded.note", (agent, time.time(), status, note))

    def skip(self, mint, agent, reason, until=None):
        now = time.time()
        self.insert("skips", {"mint": mint, "until": until or now + 24 * 3600, "agent": agent, "reason": reason, "ts": now})

    def skipped(self, mint, now=None):
        return self.one("SELECT * FROM skips WHERE mint=? AND until>? ORDER BY ts DESC", (mint, now or time.time()))

    def open_positions(self):
        return self.q("SELECT * FROM positions WHERE status IN ('open','pending','exiting') ORDER BY id")

    def coin_name(self, mint):
        c = self.one("SELECT name, symbol FROM coins WHERE mint=?", (mint,)) or {}
        return c.get("symbol") or c.get("name") or (mint[:4] + "…" + mint[-3:])
