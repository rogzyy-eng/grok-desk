"""Frontend contract: a 401 must leave the login screen up, and the code box
must not auto-capitalize on iOS. install.sh must keep live secrets and the DB."""
import re
from pathlib import Path

import config

ROOT = Path(config.ROOT)
JS = (ROOT / "web" / "app.js").read_text()
INSTALL = (ROOT / "deploy" / "install.sh").read_text()


def test_401_leaves_login_visible_and_input_is_plain():
    assert "function rejectAuth()" in JS
    assert 'login("Access code was rejected.")' in JS
    assert "e.authRejected = true" in JS
    assert "throw new Error(\"401\")" not in JS.split("function rejectAuth()")[0]
    # every errorBox catch bails on the sentinel so it cannot cover the login screen
    catches = re.findall(r"catch \(e\) \{[^}]*errorBox\(e\)[^}]*\}", JS)
    assert len(catches) >= 6, catches
    for block in catches:
        assert "e.authRejected" in block, block
    tag = re.search(r'<input id="tok"[^>]*>', JS)
    assert tag, "login input missing"
    for attr in ('autocapitalize="off"', 'autocorrect="off"', 'spellcheck="false"', 'autocomplete="off"'):
        assert attr in tag.group(0), attr
    assert '$("#tok").value.trim()' in JS


def test_install_sh_rerun_keeps_desk_env_and_db():
    assert '[ ! -s "$ETC/desk.env" ]' in INSTALL
    assert "keeping existing $ETC/desk.env" in INSTALL
    # the generator only runs in the missing-file branch
    gen = INSTALL.split("if [ ! -s")[1].split("else")[0]
    assert "APP_TOKEN=" in gen
    after = INSTALL.split("else", 1)[1]
    assert "APP_TOKEN=" not in after.split("cat > /usr/local/sbin/grok-desk-url")[0]
    assert 'rm -rf "$DATA"' not in INSTALL
    assert "rm -rf $DATA" not in INSTALL
    assert "DESK_DATA_DIR=/var/lib/grok-desk" in (ROOT / "deploy" / "grok-desk.service").read_text()
