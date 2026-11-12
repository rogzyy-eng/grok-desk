"""secrets grep: no secret in the repo, web/, logs, or any API response."""
import re
import secrets
from pathlib import Path

from fastapi.testclient import TestClient

import config

ROOT = Path(config.ROOT)
PATTERNS = [re.compile(p) for p in (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"APP_TOKEN\s*=\s*['\"]?[A-Za-z0-9_\-]{16,}",
            r"VAPID_PRIVATE_KEY\s*=\s*['\"]?[A-Za-z0-9_\-]{16,}", r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b",
            r"TG_TOKEN\s*=\s*['\"]?\d+:", r"TYPESAFE_API_KEY\s*=\s*['\"]?[A-Za-z0-9_\-]{16,}")]


def files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(x in p.parts for x in (".venv", "__pycache__", ".pytest_cache", "proof", "data")) \
                and p.suffix not in (".png", ".db", ".db-wal", ".db-shm"):
            yield p


def test_repo_has_no_secrets():
    hits = []
    for p in files():
        txt = p.read_text(errors="ignore")
        for pat in PATTERNS:
            if pat.search(txt):
                hits.append(f"{p.relative_to(ROOT)}: {pat.pattern}")
    assert not hits, hits


def test_runtime_secrets_never_leak(db, monkeypatch, tmp_path):
    import webpush
    tok = secrets.token_urlsafe(48)
    priv, pub = webpush.generate_vapid_pair()
    monkeypatch.setattr(config, "APP_TOKEN", tok)
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", priv)
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", pub)
    tg, jev = "123456789:" + secrets.token_urlsafe(26), secrets.token_urlsafe(32)
    monkeypatch.setattr(config, "TG_TOKEN", tg)
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", jev)
    from api import make_app
    from conftest import FakeSrc
    from main import Desk
    desk = Desk(db=db, src=FakeSrc())
    c = TestClient(make_app(desk))
    h = {"Authorization": "Bearer " + tok}
    bodies = [c.get(p, headers=h).text for p in ("/api/state", "/api/settings", "/api/account", "/api/agents",
              "/api/activity", "/api/report", "/api/health", "/api/push", "/", "/app.js", "/sw.js")]
    bodies.append(c.post("/api/command", json={"text": "status"}, headers=h).text)
    for b in bodies:
        assert tok not in b and priv not in b and tg not in b and jev not in b
    assert pub in c.get("/api/push/key", headers=h).text        # the PUBLIC key is meant to be shared
    for p in list(files()) + list((ROOT / "logs").rglob("*")):
        if p.is_file():
            t = p.read_text(errors="ignore")
            assert tok not in t and priv not in t and tg not in t and jev not in t
    assert not (ROOT / ".env").exists() and not (ROOT / "desk.env").exists()
