"""390-px iPhone screenshots of the PWA against a DEMO database. Usage: screens.py BASE_URL TOKEN OUTDIR"""
import sys
from playwright.sync_api import sync_playwright

base, tok, out = sys.argv[1], sys.argv[2], sys.argv[3]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1")
    pg = ctx.new_page()
    pg.goto(base + "/"); pg.wait_for_timeout(800); pg.screenshot(path=f"{out}/01_login.png")
    pg.goto(base + "/index.html#t=" + tok); pg.wait_for_timeout(1500)
    assert "#t=" not in pg.url, "token must be wiped from the address bar"
    pg.screenshot(path=f"{out}/02_home.png", full_page=True)
    pg.click("#tabs button[data-tab=agents]"); pg.wait_for_timeout(900); pg.screenshot(path=f"{out}/03_agents.png", full_page=True)
    pg.click("#tabs button[data-tab=activity]"); pg.wait_for_timeout(900); pg.screenshot(path=f"{out}/04_activity.png", full_page=True)
    pg.click("#tabs button[data-tab=account]"); pg.wait_for_timeout(900); pg.screenshot(path=f"{out}/05_account.png", full_page=True)
    pg.click("#tabs button[data-tab=home]"); pg.wait_for_timeout(900)
    pg.click("[data-coin]"); pg.wait_for_timeout(1200); pg.screenshot(path=f"{out}/06_coin_candles.png", full_page=True)
    pg.click("#sheet [data-audit]"); pg.wait_for_timeout(1200); pg.screenshot(path=f"{out}/07_auditor_disc_demo.png", full_page=True)
    pg.evaluate("window.__desk.audit('A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump')"); pg.wait_for_timeout(1200)
    pg.screenshot(path=f"{out}/08_auditor_disc_cybron_replay.png", full_page=True)
    pg.goto(base + "/"); pg.evaluate("localStorage.setItem('desk_token','wrong-token-wrong-token-xx')"); pg.reload(); pg.wait_for_timeout(1200)
    pg.screenshot(path=f"{out}/09_bad_token_error.png")
    errs = []
    b.close()
print("ok")
