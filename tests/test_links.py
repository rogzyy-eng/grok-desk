"""1 test_links — every FOMO URL has r=savipww; unknown chain -> SIGNUP_URL (logged); no other builder."""
import logging
from pathlib import Path

import links


def test_coin_url_every_chain():
    for chain, cid in {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}.items():
        assert links.coin_url("ADDR", chain) == f"https://fomo.family/coin?address=ADDR&chainId={cid}&r=savipww&source=share_link"
    assert links.coin_url("ADDR") == links.coin_url("ADDR", "solana")   # pumpfun coins are Solana


def test_unknown_chain_logged_and_signup(caplog):
    with caplog.at_level(logging.WARNING, logger="links"):
        assert links.coin_url("X", "ethereum") == "https://fomo.family/r/savipww"
    assert "unknown chain" in caplog.text


def test_profile_url():
    assert links.profile_url("@rogzyy3") == "https://fomo.family/profile/rogzyy3?source=share_link&r=savipww"


def test_no_fomo_url_elsewhere_no_shorteners():
    root = Path(links.__file__).parent
    files = [p for p in root.rglob("*") if p.suffix in (".py", ".js", ".html") and "tests" not in p.parts
             and ".venv" not in p.parts and p.name != "links.py"]
    for p in files:
        t = p.read_text()
        assert "fomo.family" not in t, p
        for bad in ("bit.ly", "t.co/", "tinyurl", "goo.gl"):
            assert bad not in t, (p, bad)
    assert "r=savipww" in links.coin_url("a") and "r=savipww" in links.profile_url("b")
    assert links.SIGNUP_URL.endswith("/r/savipww")
