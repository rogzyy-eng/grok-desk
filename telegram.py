"""telegram.py — Telegram bot: long polling, owner only (STEP 9). Osman SKIPPED Telegram in STEP 2,
so it is off unless TG_TOKEN and TG_CHAT_ID are set and desk.notifier = telegram."""
import asyncio
import logging

import httpx

import config

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"


def configured():
    return bool(config.TG_TOKEN and config.TG_CHAT_ID)


def enabled(db=None):
    return configured() and (db is None or db.get("desk.notifier") == "telegram")


def keyboard(buttons):
    row = []
    for b in buttons or []:
        if b.get("url"):
            row.append({"text": b["label"], "url": b["url"]})          # exact links.py string
        elif b.get("action"):
            row.append({"text": b["label"], "callback_data": f"{b['action']}:{b.get('mint', '')}"[:64]})
    return {"inline_keyboard": [row]} if row else None


async def send(text, buttons=None):
    if not configured():
        return False
    body = {"chat_id": config.TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    kb = keyboard(buttons)
    if kb:
        body["reply_markup"] = kb
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(API.format(token=config.TG_TOKEN, method="sendMessage"), json=body)
        return r.status_code == 200


async def poll_commands(db, handler, stop):
    if not enabled(db):
        log.info("telegram off (skipped in STEP 2)")
        return
    offset = 0
    async with httpx.AsyncClient(timeout=40) as c:
        while not stop.is_set():
            try:
                r = await c.get(API.format(token=config.TG_TOKEN, method="getUpdates"),
                                params={"timeout": 30, "offset": offset})
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    cb = u.get("callback_query")
                    m = u.get("message") or (cb or {}).get("message") or {}
                    if str((m.get("chat") or {}).get("id")) != str(config.TG_CHAT_ID):
                        continue
                    text = (cb["data"].replace(":", " ", 1) if cb else m.get("text") or "")
                    reply = await handler(text, source="telegram")
                    await send(reply.get("text", ""), reply.get("buttons"))
            except Exception as e:
                log.warning("telegram poll error %s", type(e).__name__)
                await asyncio.sleep(5)
