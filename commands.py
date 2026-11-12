"""commands.py — STEP 9. Same handler for the app, Telegram (if ever on) and Grok Bot.
No command ever places an order: buy/sell only return the FOMO button; bought/sold record what
Osman did by hand. Every coin mentioned carries its links.coin_url."""
import re
import time

import config
import fees
import links
from notifier import buy_button, sell_button

HELP = ("commands: buy X · sell X · why X · pause <agent> · resume <agent> · set stop to -8% · "
        "stack 1240 · status · paper · live · invite · report · bought X · sold X")


class GateError(Exception):
    pass


def live_gate(db):
    missing = []
    if int(db.get("desk.paper_nights", 0) or 0) < 1:
        missing.append("at least 1 full PAPER night")
    if not (db.get("desk.layer8_proof") or {}).get("passed"):
        missing.append("LAYER 8 proof")
    return missing


def set_mode(db, mode, confirm="", who="app"):
    mode = (mode or "").upper()
    if mode not in ("PAPER", "LIVE"):
        raise GateError("mode must be PAPER or LIVE")
    if mode == "LIVE":
        missing = live_gate(db)
        if missing:
            raise GateError("LIVE locked: needs " + " and ".join(missing))
        if confirm != "LIVE":
            raise GateError("type LIVE to confirm")
    elif db.get("desk.mode") != "PAPER":
        db.set("desk.paper_since", time.time(), who)
    db.set("desk.mode", mode, who)
    return mode


PROTECTED = {"desk.mode", "desk.paper_nights", "desk.paper_since", "desk.layer8_proof", "desk.last_night_closed",
             "desk.paused_agents", "desk.layer8_down_kills", "desk.live_stack", "desk.last_page_url"}
CHOICES = {"desk.notifier": ("webpush", "telegram", "none"), "analyst.mode": ("rules", "jev"),
           "executor.below_min": ("bump", "skip")}


def coerce(old, raw):
    raw = str(raw).strip()
    if isinstance(old, bool):
        if raw.lower() in ("true", "on", "1", "yes", "evet"):
            return True
        if raw.lower() in ("false", "off", "0", "no", "hayir", "hayır"):
            return False
        raise ValueError("expected on/off")
    if isinstance(old, (int, float)):
        r = raw.replace("%", "").replace("$", "").replace(",", "").strip()
        mult = 1000 if r.lower().endswith("k") else 1
        f = abs(float(r.rstrip("kK"))) * mult      # "-8%" for a stop means 8
        return int(f) if isinstance(old, int) and f.is_integer() else f
    return raw


def set_setting(db, key, raw, who="app"):
    key = config.ALIASES.get(key.lower().strip(), key.strip())
    if key in PROTECTED:
        raise GateError(f"{key} cannot be set directly")
    valid = {f"{s}.{k}" for s, v in config.DEFAULTS.items() for k in v}
    if key not in valid:
        raise GateError(f"unknown setting {key}")
    old = db.get(key)
    val = coerce(old, raw) if not (isinstance(raw, (int, float, bool)) and not isinstance(old, str)) else raw
    if key in CHOICES and val not in CHOICES[key]:
        raise GateError(f"{key} must be one of {', '.join(CHOICES[key])}")
    if key == "executor.min_ticket_usd" and float(val) < fees.MIN_TRADE:
        raise GateError(f"min ticket cannot be below FOMO's ${fees.MIN_TRADE:.2f}")
    if key == "auditor.enabled" and val is False and str(who) != "app-confirmed-twice" and who not in ("test",):
        raise GateError("turning the Auditor off needs a second confirmation")
    db.set(key, val, who)
    return key, old, val


def pretty(key, v):
    if key == "risk.stop_pct":
        return f"-{v:g}%"
    if key.endswith("_pct"):
        return f"{v:g}%"
    return str(v)


def find(db, ref):
    ref = (ref or "").strip().lstrip("$")
    if not ref:
        return []
    r = db.q("SELECT * FROM coins WHERE mint=?", (ref,))
    return r or db.q("SELECT * FROM coins WHERE upper(symbol)=upper(?) OR upper(name)=upper(?) ORDER BY updated_at DESC", (ref, ref))


def resolve(db, ref):
    """-> (coin, reply-if-ambiguous)."""
    m = find(db, ref)
    if not m:
        if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", ref or ""):
            return {"mint": ref, "symbol": None, "name": None, "chain": "solana"}, None
        return None, {"ok": False, "text": f"I don't know {ref}. Send me its mint."}
    if len(m) > 1:
        return None, {"ok": False, "text": f"{ref} matches {len(m)} coins, which one?\n" +
                      "\n".join(f"{c['symbol'] or c['name']} {c['mint']} {links.coin_url(c['mint'])}" for c in m)}
    return m[0], None


def status(db):
    hb = {r["agent"]: {"age_s": round(time.time() - r["ts"]), "status": r["status"], "note": r["note"]}
          for r in db.q("SELECT * FROM heartbeats")}
    return {"mode": db.get("desk.mode"), "paused_agents": db.get("desk.paused_agents") or [],
            "balance": round(db.equity(), 2), "cash": round(db.cash(), 2), "open_positions": len(db.open_positions()),
            "agents": hb, "paper_nights": db.get("desk.paper_nights"), "notifier": db.get("desk.notifier"),
            "live_missing": live_gate(db)}


async def handle(desk, text, source="app"):
    db = desk.db
    raw = (text or "").strip()
    parts = raw.lstrip("/").split()
    if not parts:
        return {"ok": False, "text": HELP}
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if raw == "LIVE":
            set_mode(db, "LIVE", "LIVE", source)
            return {"ok": True, "text": "LIVE on. I never place orders: every BUY/SELL is a FOMO button you tap."}
        if cmd == "status":
            s = status(db)
            btns, lines = [], [f"{s['mode']} · balance ${s['balance']:.2f} · {s['open_positions']} open"]
            for p in db.open_positions():
                n = db.coin_name(p["mint"])
                lines.append(f"{n} ${p['size_usd']:.2f} · stop -{p['stop_pct']:g}%{' · volatil' if p.get('volatile') else ''} · {links.coin_url(p['mint'])}")
                btns.append(sell_button(p["mint"]))
            return {"ok": True, "text": "\n".join(lines), "buttons": btns, "data": s}
        if cmd in ("pause", "resume"):
            who = (args[0].lower() if args else "")
            names = list(config.AGENTS) if who in ("", "all", "desk") else [who]
            if any(n not in config.AGENTS for n in names):
                return {"ok": False, "text": "pause/resume scanner|auditor|analyst|executor|risk|all"}
            cur = set(db.get("desk.paused_agents") or [])
            if cmd == "pause":
                if "risk" in names and db.open_positions():
                    names = [n for n in names if n != "risk"]
                    if who == "risk":
                        return {"ok": False, "text": "Risk can't be paused while a position is open"}
                cur |= set(names)
            else:
                cur -= set(names)
            db.set("desk.paused_agents", sorted(cur), source)
            return {"ok": True, "text": f"{cmd}d: {', '.join(names)}"}
        if cmd == "paper":
            set_mode(db, "PAPER", who=source)
            return {"ok": True, "text": "PAPER mode"}
        if cmd == "live":
            missing = live_gate(db)
            if missing:
                return {"ok": False, "text": "LIVE locked: needs " + " and ".join(missing)}
            return {"ok": True, "text": "Type LIVE to switch. I never place orders."}
        if cmd == "set":
            m = re.match(r"set\s+(.+?)\s+(?:to\s+)?(\S+)$", raw.lstrip("/"), re.I)
            if not m:
                return {"ok": False, "text": "usage: set stop to -8%"}
            alias = m.group(1).strip().lower()
            key, old, new = set_setting(db, m.group(1), m.group(2), source)
            short = alias if alias in config.ALIASES else key
            return {"ok": True, "text": f"{short} {pretty(key, old)} -> {pretty(key, new)}"}
        if cmd == "stack":
            if not args:
                return {"ok": True, "text": f"LIVE stack ${float(db.get('desk.live_stack') or 0):.2f}"}
            amt = float(args[0].replace("$", "").replace(",", ""))
            db.set("desk.live_stack", amt, source)
            return {"ok": True, "text": f"LIVE stack ${amt:.2f}"}
        if cmd == "invite":
            return {"ok": True, "text": links.SIGNUP_URL, "buttons": [{"label": "Join FOMO", "url": links.SIGNUP_URL}]}
        if cmd == "report":
            url = db.get("desk.last_page_url")
            return {"ok": bool(url), "text": url or "no morning Page yet"}
        if cmd in ("why", "buy", "sell", "bought", "sold"):
            coin, amb = resolve(db, " ".join(args))
            if amb:
                return amb
            if coin is None:
                return {"ok": False, "text": HELP}
            return await globals()["cmd_" + cmd](desk, coin, args, source)
        return {"ok": False, "text": HELP}
    except (GateError, ValueError) as e:
        return {"ok": False, "text": str(e)}


async def cmd_why(desk, coin, args, source):
    db, m = desk.db, coin["mint"]
    d = db.one("SELECT * FROM decisions WHERE mint=? AND decision IN ('kill','reject','skip','expired') ORDER BY id DESC", (m,))
    name = db.coin_name(m)
    url = links.coin_url(m)
    if not d:
        return {"ok": True, "text": f"Nothing stopped {name}. {url}", "buttons": [buy_button(m)]}
    when = time.strftime("%H:%M", time.localtime(d["ts"]))
    return {"ok": True, "text": f"{name}: stopped by {d['agent'].title()} · {d['reason']} · {when}\n{url}", "buttons": [buy_button(m)]}


async def cmd_buy(desk, coin, args, source):
    db, m = desk.db, coin["mint"]
    sig = db.one("SELECT * FROM signals WHERE mint=? AND side='buy' AND ts>? ORDER BY id DESC", (m, time.time() - 18 * 3600))
    name = db.coin_name(m)
    if sig:
        await desk.notifier.push("buy", f"Buy {name} · ${sig['size_usd']:.2f}", mint=m, buttons=[buy_button(m)])
        return {"ok": True, "text": f"Buy {name} · ${sig['size_usd']:.2f}\n{links.coin_url(m)}", "buttons": [buy_button(m)]}
    if not db.one("SELECT 1 FROM coins WHERE mint=?", (m,)):
        db.insert("coins", {"mint": m, "status": "manual", "migrated_at": time.time(), "updated_at": time.time()})
    pair = None
    try:
        import fields
        pair = fields.best_pair(await desk.src.dex_pairs([m]))
    except Exception:
        pass
    scan = {"pool": (pair or {}).get("pair_address"), "created_ms": (pair or {}).get("created_ms")}
    out = await desk.auditor.audit(m, scan)
    lines = [f"L{l['n']} {l['name']}: {'✅' if l['pass'] else '❌' if l['pass'] is False else '—'} {l['value']}" for l in out["layers"]]
    return {"ok": True, "text": "\n".join(lines) + f"\nnot sized, not on the desk\n{links.coin_url(m)}",
            "buttons": [buy_button(m)], "data": out}


async def cmd_sell(desk, coin, args, source):
    db, m = desk.db, coin["mint"]
    p = db.one("SELECT * FROM positions WHERE mint=? AND status IN ('open','exiting') ORDER BY id DESC", (m,))
    if not p:
        return {"ok": False, "text": f"No open {db.coin_name(m)} position. {links.coin_url(m)}"}
    return {"ok": True, "text": f"Sell {db.coin_name(m)}\n{links.coin_url(m)}", "buttons": [sell_button(m)]}


async def cmd_bought(desk, coin, args, source):
    db, m = desk.db, coin["mint"]
    p = db.one("SELECT * FROM positions WHERE mint=? AND status='pending' ORDER BY id DESC", (m,))
    if not p:
        return {"ok": False, "text": "no pending buy for that coin"}
    last = db.one("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC", (m,))
    price = (last or {}).get("price") or p["entry_price"]
    fee = p["entry_fee"]
    db.update("positions", "id=?", (p["id"],), {"status": "open", "entry_price": price, "peak_price": price,
              "stop_price": price * (1 - p["stop_pct"] / 100), "qty": (p["size_usd"] - fee) / price, "opened_at": time.time()})
    db.update("signals", "id=?", (p["signal_id"],), {"status": "bought"})
    name = db.coin_name(m)
    desk.bus.activity("bought", m, "executor", f"Bought {name}", "Executor · you bought on FOMO", f"${p['size_usd']:.2f}")
    return {"ok": True, "text": f"Bought {name} recorded. Risk is guarding it. {links.coin_url(m)}"}


async def cmd_sold(desk, coin, args, source):
    db, m = desk.db, coin["mint"]
    p = db.one("SELECT * FROM positions WHERE mint=? AND status IN ('exiting','open') ORDER BY id DESC", (m,))
    if not p:
        return {"ok": False, "text": "no open position for that coin"}
    last = db.one("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC", (m,))
    price = p["exit_price"] or (last or {}).get("price") or p["entry_price"]
    gross = (p["qty"] or 0) * price
    xfee = fees.side_fee(max(gross, fees.MIN_TRADE), float(db.get("desk.fees_ref_discount", 0.10)))
    pnl = gross - xfee - p["size_usd"]
    db.update("positions", "id=?", (p["id"],), {"status": "closed", "closed_at": time.time(), "exit_price": price,
              "exit_reason": p["exit_reason"] or "manual", "pnl_usd": round(pnl, 4), "fees_usd": round((p["entry_fee"] or 0) + xfee, 4)})
    return {"ok": True, "text": f"{db.coin_name(m)} closed · {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}. {links.coin_url(m)}"}
