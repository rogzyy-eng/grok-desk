"""Live smoke test: decode real pools over the public Solana websocket and compare with DexScreener.
python tools/ws_smoke.py [seconds]  (prints prices only; no keys, no orders)"""
import asyncio
import sys
import time

import httpx

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
import config  # noqa: E402
from onchain import Watcher  # noqa: E402
from sources import Sources  # noqa: E402


async def main(secs=40):
    async with httpx.AsyncClient(timeout=20) as h:
        d = (await h.get("https://api.dexscreener.com/latest/dex/search?q=pumpswap%20SOL")).json()
        e = (await h.get("https://api.dexscreener.com/latest/dex/search?q=pumpfun%20SOL")).json()
    pairs = [p for p in d["pairs"] + e["pairs"] if p["chainId"] == "solana" and p.get("quoteToken", {}).get("symbol") == "SOL"]
    pick = {}
    for p in sorted(pairs, key=lambda p: -((p.get("volume") or {}).get("m5") or 0)):
        if p["dexId"] in ("pumpswap", "pumpfun", "raydium") and p["dexId"] not in pick:
            pick[p["dexId"]] = p
    src = Sources(config.rpc_url())
    seen = {}

    async def on_update(mint, st, role):
        seen[mint] = seen.get(mint, 0) + 1

    w = Watcher(config.ws_url(), src.rpc, on_update)
    runner = asyncio.create_task(w.run())
    for dex, p in pick.items():
        st = await w.watch(p["baseToken"]["address"], p["pairAddress"])
        print(dex, p["baseToken"]["symbol"], p["pairAddress"], "->", st.kind if st else w.unsupported.get(p["baseToken"]["address"]))
        await asyncio.sleep(1)
    t0 = time.time()
    while time.time() - t0 < secs:
        await asyncio.sleep(2)
    async with httpx.AsyncClient(timeout=20) as h:
        for dex, p in pick.items():
            m = p["baseToken"]["address"]
            st = w.state(m)
            if not st:
                continue
            r = (await h.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{p['pairAddress']}")).json()
            q = (r.get("pairs") or [r.get("pair")])[0]
            dn = float(q["priceNative"])
            print(f"{dex:8} {p['baseToken']['symbol']:12} ws={st.price_native:.6e} SOL dex={dn:.6e} SOL "
                  f"diff={(st.price_native / dn - 1) * 100:+.2f}% ws_updates={seen.get(m, 0)} quote_reserve={st.quote_reserve:.2f} SOL "
                  f"usd={st.price_native * float(q['priceUsd']) / dn:.6g} dexUsd={q['priceUsd']} creator_watched={bool(st.dev_start)}")
    print("connected:", w.connected, "reconnects:", w.reconnects)
    w.stopped = True
    runner.cancel()
    await src.close()


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 40))
