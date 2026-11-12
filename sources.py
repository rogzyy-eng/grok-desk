"""sources.py — network clients (DexScreener, RugCheck, Solana RPC, PumpPortal).
ReplaySources serves logs/raw/ samples so the whole pipeline can run offline (test_replay)."""
import asyncio
import json
import logging
from pathlib import Path

import httpx

import config

log = logging.getLogger("sources")


class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class Sources:
    def __init__(self, rpc_url=None, timeout=15.0):
        self.rpc_url = rpc_url or config.DEFAULTS["desk"]["rpc_url"]
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "grok-desk/1.0"})

    async def close(self):
        await self.client.aclose()

    async def _get_json(self, url):
        try:
            r = await self.client.get(url)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise FetchError(f"{type(e).__name__}") from e
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code}", r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise FetchError("bad json") from e

    async def dex_pairs(self, mints):
        mints = [m for m in mints if m][:30]
        return await self._get_json(config.DEXSCREENER_TOKENS.format(mints=",".join(mints)))

    async def rc_report(self, mint):
        return await self._get_json(config.RUGCHECK_REPORT.format(mint=mint))

    async def rc_graph(self, mint):
        """Returns the JSON body; raises FetchError on HTTP error / timeout."""
        return await self._get_json(config.RUGCHECK_GRAPH.format(mint=mint))

    async def rpc(self, method, params, timeout=30.0):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            r = await self.client.post(self.rpc_url, json=body, timeout=timeout)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise FetchError(type(e).__name__) from e
        if r.status_code != 200:
            raise FetchError(f"RPC HTTP {r.status_code}", r.status_code)
        data = r.json()
        if "error" in data:
            raise FetchError(f"RPC {data['error'].get('code')}: {data['error'].get('message')}",
                             data["error"].get("code"))
        return data

    async def mint_info(self, mint):
        return await self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])

    async def token_supply(self, mint):
        return await self.rpc("getTokenSupply", [mint])

    async def largest_accounts(self, mint):
        return await self.rpc("getTokenLargestAccounts", [mint])

    async def jev(self, api_key, body, timeout):
        try:
            r = await self.client.post(config.JEV_URL, json=body, timeout=timeout,
                                       headers={"Authorization": f"Bearer {api_key}"})
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise FetchError(type(e).__name__) from e
        if r.status_code != 200:
            raise FetchError(f"Jev HTTP {r.status_code}", r.status_code)
        return r.json()

    async def token_accounts(self, program, mint):
        return await self.rpc("getProgramAccounts", [program, {"encoding": "jsonParsed",
                              "filters": [{"memcmp": {"offset": 0, "bytes": mint}}]}], timeout=60)


class ReplaySources:
    """Offline source backed by logs/raw/ samples (CYBRON). Time is supplied by the test."""
    def __init__(self, raw_dir=None, graph_mode="file", fail_graph=False):
        self.raw = Path(raw_dir or config.RAW_DIR)
        self.graph_mode = graph_mode
        self.fail_graph = fail_graph
        self.calls = []

    def _load(self, name):
        return json.loads((self.raw / name).read_text())

    async def close(self):
        pass

    async def dex_pairs(self, mints):
        self.calls.append("dex")
        return self._load("dexscreener_pair.json")

    async def rc_report(self, mint):
        self.calls.append("report")
        return self._load("rugcheck_report.json")

    async def rc_graph(self, mint):
        self.calls.append("graph")
        if self.fail_graph:
            raise FetchError("HTTP 502")
        if self.graph_mode == "empty":
            return self._load("rugcheck_insiders_graph.json")   # observed 200 []
        return self._load(f"rugcheck_insiders_graph_{mint}.json")

    async def mint_info(self, mint):
        self.calls.append("mint_info")
        return self._load("rpc_getAccountInfo.json")

    async def token_supply(self, mint):
        self.calls.append("supply")
        return self._load("rpc_getTokenSupply.json")

    async def largest_accounts(self, mint):
        self.calls.append("largest")
        raise FetchError("RPC 429: Too many requests (as sampled)", 429)

    async def token_accounts(self, program, mint):
        self.calls.append(f"gpa:{program[:8]}")
        if program == config.TOKEN_2022_PROGRAM:
            return self._load("rpc_getProgramAccounts_token2022_actual_owner.json")
        return self._load("rpc_getProgramAccounts_token.json")
