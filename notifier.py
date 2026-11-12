"""notifier.py — one notifier, channel from STEP 2 (here: webpush). Takes every FOMO URL from links.py
and puts it on the button; a channel without buttons gets the full URL as a line under the message.
PAPER pushes start with "PAPER · ". Rejects are pushed only if desk.push_rejects."""
import logging
import time

import links
import telegram
import webpush

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, db, bus):
        self.db, self.bus = db, bus
        self.sent = []

    def channel(self):
        ch = self.db.get("desk.notifier", "webpush")
        if ch == "telegram":
            return "telegram" if telegram.configured() else "none"
        if ch == "webpush":
            return "webpush" if webpush.enabled() else "none"
        return "none"

    async def push(self, kind, text, mint=None, buttons=None, force=False):
        """buttons: [{"label": "Buy on FOMO", "url": links.coin_url(...)}, {"label": "Sold", "action": "sold", "mint": m}]"""
        if kind == "rejected" and not self.db.get("desk.push_rejects", False) and not force:
            rec = {"kind": kind, "text": text, "result": "suppressed"}
            self.sent.append(rec)
            return rec
        if self.db.get("desk.mode") == "PAPER" and not text.startswith("PAPER · "):
            text = "PAPER · " + text
        buttons = list(buttons or [])
        url = next((b["url"] for b in buttons if b.get("url")), None)
        payload = {"title": "Grok Bot", "body": text, "url": url or "/", "mint": mint, "kind": kind,
                   "buttons": buttons, "ts": time.time()}
        ch = self.channel()
        rec = {**payload, "channel": ch, "result": None}
        try:
            if ch == "webpush":
                rec["result"] = await webpush.send_all(self.db, payload)
            elif ch == "telegram":
                rec["result"] = await telegram.send(text, buttons)
            else:
                rec["result"] = "no channel"
        except Exception as e:
            rec["result"] = f"error {type(e).__name__}"
            log.warning("push error %s", type(e).__name__)
        if mint:
            self.db.x("UPDATE signals SET pushed_at=? WHERE id=(SELECT max(id) FROM signals WHERE mint=?)", (time.time(), mint))
        self.sent.append(rec)
        self.bus.publish("push", {"kind": kind, "text": text, "url": url, "mint": mint})
        return rec

    @staticmethod
    def plain(text, buttons):
        """For a channel without buttons: the full links.py URL as a line under the message."""
        return text + "".join(f"\n{b['url']}" for b in buttons or [] if b.get("url"))


def buy_button(mint, chain="solana"):
    return {"label": "Buy on FOMO", "url": links.coin_url(mint, chain)}


def sell_button(mint, chain="solana"):
    return {"label": "Sell on FOMO", "url": links.coin_url(mint, chain)}
