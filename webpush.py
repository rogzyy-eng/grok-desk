"""webpush.py — Web Push (VAPID) to db.push_subs. 410 Gone = delete that subscription."""
import asyncio
import base64
import json
import logging
import time

import config

log = logging.getLogger("webpush")


def enabled():
    return bool(config.VAPID_PRIVATE_KEY and config.VAPID_PUBLIC_KEY)


def public_key():
    return config.VAPID_PUBLIC_KEY


def generate_vapid_pair():
    """(private_b64url_raw32, public_b64url_uncompressed65). Run on the server by deploy/install.sh."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    priv = k.private_numbers().private_value.to_bytes(32, "big")
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    return b(priv), b(pub)


def _send_one(row, payload):
    from pywebpush import webpush
    sub = {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
    return webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=config.VAPID_PRIVATE_KEY,
                   vapid_claims={"sub": config.VAPID_SUB}, ttl=3600, timeout=10)


async def send_all(db, payload):
    if not enabled():
        return {"sent": 0, "failed": 0, "disabled": True}
    sent = failed = 0
    for row in db.q("SELECT * FROM push_subs"):
        try:
            await asyncio.to_thread(_send_one, row, payload)
            sent += 1
        except Exception as e:
            failed += 1
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                db.x("DELETE FROM push_subs WHERE endpoint=?", (row["endpoint"],))
            log.warning("push failed status=%s", status)
    return {"sent": sent, "failed": failed}


def save_subscription(db, sub):
    ep = sub.get("endpoint") if isinstance(sub, dict) else None
    keys = sub.get("keys") if isinstance(sub, dict) else None
    if not ep or not str(ep).startswith("https://") or not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("bad subscription")
    db.x("INSERT INTO push_subs VALUES(?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, "
         "auth=excluded.auth", (ep, keys["p256dh"], keys["auth"], time.time()))
