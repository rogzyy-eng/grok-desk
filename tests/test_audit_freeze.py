"""5 Auditor on a mint with freeze authority set: red at layer 2, layers 3-8 "not checked"."""
import asyncio
import copy

from agents.auditor import Auditor
from bus import Bus
from conftest import no_sleep
from notifier import Notifier
from sources import ReplaySources

MINT = "A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump"
CREATED = 1794428021000
POOL = "DUxG3Y9BAe6kvz2kkXYtHwRpc5jDj2pSh7xk9bYyhwwD"


class FreezeSrc(ReplaySources):
    def __init__(self, freeze=None, **kw):
        super().__init__(**kw)
        self.freeze = freeze

    async def mint_info(self, mint):
        d = copy.deepcopy(await super().mint_info(mint))
        d["result"]["value"]["data"]["parsed"]["info"]["freezeAuthority"] = self.freeze
        return d


def run(db, src):
    a = Auditor(db, Bus(db), src, Notifier(db, Bus(db)), clock=lambda: CREATED / 1000 + 20 * 60, sleep=no_sleep)
    return asyncio.run(a.audit(MINT, {"pool": POOL, "created_ms": CREATED}))


def test_freeze_red_at_2_rest_not_checked(db):
    src = FreezeSrc(freeze="FrEeZe1111111111111111111111111111111111111")
    r = run(db, src)
    assert [l["pass"] for l in r["layers"]] == [True, False] + [None] * 6
    assert r["layers"][1]["value"].startswith("freeze authority live")
    assert all(l["value"] == "not checked" for l in r["layers"][2:])
    assert r["layersChecked"] == 2 and not r["passed"]
    assert "report" not in src.calls and "graph" not in src.calls       # later layers really not run
    assert db.one("SELECT count(*) n FROM audits WHERE pass IS NULL")["n"] == 6


def test_clean_sample_passes_8_of_8(db):
    r = run(db, FreezeSrc())
    assert [l["pass"] for l in r["layers"]] == [True] * 8, r["layers"]
    assert r["layers"][7]["value"] == "None" and r["holders"] == 138
