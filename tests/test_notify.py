"""1b test_notify — 1 PAPER push through every channel set up; each carries the exact links.py URL."""
import asyncio

import config
import links
from bus import Bus
from notifier import Notifier, buy_button


def test_webpush_paper_push_exact_link(db, monkeypatch):
    import webpush
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "x" * 43)
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "y" * 87)
    sent = []

    async def fake(_db, payload):
        sent.append(payload)
        return {"sent": 1, "failed": 0}
    monkeypatch.setattr(webpush, "send_all", fake)
    n = Notifier(db, Bus(db))
    assert n.channel() == "webpush"
    asyncio.run(n.push("bought", "Bought TST · $47.50", mint="MintA", buttons=[buy_button("MintA")]))
    p = sent[0]
    assert p["body"].startswith("PAPER · ")
    assert p["url"] == links.coin_url("MintA", "solana") and "r=savipww" in p["url"]
    assert p["buttons"][0]["url"] == links.coin_url("MintA", "solana")


def test_telegram_buttons_exact_and_plain_fallback():
    import telegram
    kb = telegram.keyboard([buy_button("M"), {"label": "Sold", "action": "sold", "mint": "M"}])
    assert kb["inline_keyboard"][0][0]["url"] == links.coin_url("M")
    assert Notifier.plain("Bought X", [buy_button("M")]).endswith("\n" + links.coin_url("M"))


def test_rejects_suppressed_by_default(db):
    n = Notifier(db, Bus(db))
    assert asyncio.run(n.push("rejected", "Rejected X"))["result"] == "suppressed"


def test_telegram_skipped_cleanly(db, monkeypatch):
    import telegram
    monkeypatch.setattr(config, "TG_TOKEN", "")
    db.set("desk.notifier", "telegram")
    n = Notifier(db, Bus(db))
    assert n.channel() == "none" and asyncio.run(n.push("bought", "x"))["result"] == "no channel"
    asyncio.run(telegram.poll_commands(db, None, asyncio.Event()))


def test_vapid_pair_shape():
    import base64
    import webpush
    priv, pub = webpush.generate_vapid_pair()
    pad = lambda s: s + "=" * (-len(s) % 4)
    assert len(base64.urlsafe_b64decode(pad(priv))) == 32 and base64.urlsafe_b64decode(pad(pub))[0] == 4
