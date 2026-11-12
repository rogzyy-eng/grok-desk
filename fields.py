"""fields.py — the ONLY place raw API keys are read. Every path comes from fields_mapping.json
(observed in logs/raw/ samples). A field not observed is returned as None — never guessed.

Extra paths used that are not in fields_mapping.json are marked SOURCE: below with where they
were observed (raw sample) or documented (official Solana jsonParsed token-account format)."""
from typing import Any, Optional


def _get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ---------- PumpPortal subscribeMigration ----------
def migration(msg: dict) -> Optional[dict]:
    if not isinstance(msg, dict) or msg.get("txType") != "migrate" or not msg.get("mint"):
        return None
    return {"mint": msg.get("mint"), "signature": msg.get("signature"),
            "pool_label": msg.get("pool"),   # "pump-amm" — a label, NOT a pool address
            "creator": None}                  # not present in migration messages


# ---------- DexScreener tokens/v1/solana/{mint} -> list of pairs ----------
PAIR_PATHS = {
    "pair_address": "pairAddress", "dex_id": "dexId", "price_usd": "priceUsd",
    "liq_usd": "liquidity.usd", "mcap": "marketCap",
    "vol_m5": "volume.m5", "vol_h1": "volume.h1", "vol_h24": "volume.h24",
    "buys_m5": "txns.m5.buys", "sells_m5": "txns.m5.sells",
    "buys_h1": "txns.h1.buys", "sells_h1": "txns.h1.sells",
    "chg_m5": "priceChange.m5", "chg_h1": "priceChange.h1", "chg_h24": "priceChange.h24",
    "created_ms": "pairCreatedAt",
}


def pair(p: dict) -> dict:
    out = {k: _get(p, v) for k, v in PAIR_PATHS.items()}
    for k in ("price_usd", "liq_usd", "mcap", "vol_m5", "vol_h1", "vol_h24", "chg_m5", "chg_h1",
              "chg_h24", "buys_m5", "sells_m5", "buys_h1", "sells_h1", "created_ms"):
        out[k] = _num(out[k])
    out["base_mint"] = _get(p, "baseToken.address")   # SOURCE: dexscreener_latest_dex_tokens.json
    out["symbol"] = _get(p, "baseToken.symbol")       # SOURCE: same
    out["name"] = _get(p, "baseToken.name")
    return out


def best_pair(pairs: Any) -> Optional[dict]:
    """Prefer dexId 'pumpswap' (the real post-migration pool), else highest liquidity.usd."""
    if isinstance(pairs, dict) and isinstance(pairs.get("pairs"), list):
        pairs = pairs["pairs"]
    if not isinstance(pairs, list) or not pairs:
        return None
    parsed = [pair(p) for p in pairs if isinstance(p, dict)]
    swaps = [p for p in parsed if p["dex_id"] == "pumpswap" and p["liq_usd"] is not None]
    pool = swaps or [p for p in parsed if p["liq_usd"] is not None]
    if not pool:
        return None
    return max(pool, key=lambda p: p["liq_usd"] or 0)


# ---------- RugCheck report ----------
def rc_creator(r: dict) -> Optional[str]:
    return r.get("creator") if isinstance(r, dict) else None


def rc_creator_balance(r: dict) -> Optional[float]:
    return _num(r.get("creatorBalance")) if isinstance(r, dict) else None


def rc_pool_vaults(r: dict) -> set:
    """SOURCE: rugcheck_report.json markets[].liquidityA / liquidityB (the pools' token vaults)."""
    out = set()
    for m in rc_markets(r):
        for k in ("liquidityA", "liquidityB", "pubkey"):
            if m.get(k):
                out.add(m[k])
    return out


def rc_markets(r: dict) -> list:
    m = r.get("markets") if isinstance(r, dict) else None
    return m if isinstance(m, list) else []


def rc_pool_pubkeys(r: dict) -> set:
    return {m.get("pubkey") for m in rc_markets(r) if m.get("pubkey")}


def rc_lp_locked_pct(r: dict, pool_address: Optional[str]) -> Optional[float]:
    """markets[].lp.lpLockedPct of the real pool (pubkey == DexScreener pairAddress)."""
    for m in rc_markets(r):
        if pool_address and m.get("pubkey") == pool_address:
            return _num(_get(m, "lp.lpLockedPct"))
    return None


def rc_top_holders(r: dict) -> list:
    th = r.get("topHolders") if isinstance(r, dict) else None
    return [{"address": h.get("address"), "owner": h.get("owner"), "pct": _num(h.get("pct")),
             "insider": h.get("insider")} for h in (th or []) if isinstance(h, dict)]


# ---------- RugCheck insiders graph ----------
def graph_bundled(graph: Any) -> Optional[int]:
    """Decision (decisions.md): 200 [] => None (0 bundled). Otherwise the biggest network,
    counting nodes with holdings > 0. Returns None for an empty graph."""
    if not isinstance(graph, list) or not graph:
        return None
    best = 0
    for net in graph:
        nodes = net.get("nodes") if isinstance(net, dict) else None
        n = sum(1 for nd in (nodes or []) if isinstance(nd, dict) and (_num(nd.get("holdings")) or 0) > 0)
        best = max(best, n)
    return best


# ---------- RPC getAccountInfo(mint, jsonParsed) ----------
def rpc_mint_info(resp: dict) -> dict:
    base = "result.value.data.parsed.info."
    value = _get(resp, "result.value")
    return {
        "found": isinstance(value, dict),
        "mint_authority": _get(resp, base + "mintAuthority"),     # null = revoked
        "freeze_authority": _get(resp, base + "freezeAuthority"), # null = revoked
        "supply": _num(_get(resp, base + "supply")),
        "decimals": _get(resp, base + "decimals"),
        "program_owner": _get(resp, "result.value.owner"),
    }


# ---------- RPC getProgramAccounts(token program, memcmp mint) ----------
def rpc_token_accounts(resp: dict) -> list:
    """SOURCE: rpc_getProgramAccounts_token2022_actual_owner.json:
    result[].account.data.parsed.info.{owner,tokenAmount.amount}.
    delegate / delegatedAmount: official Solana jsonParsed token-account format; the keys are
    only present when a delegate is set (none was set in the sample)."""
    out = []
    for a in (resp.get("result") or []) if isinstance(resp, dict) else []:
        info = _get(a, "account.data.parsed.info") or {}
        out.append({"pubkey": a.get("pubkey"), "owner": info.get("owner"),
                    "amount": _num(_get(info, "tokenAmount.amount")) or 0.0,
                    "delegate": info.get("delegate"),
                    "delegated": _num(_get(info, "delegatedAmount.amount")) or 0.0,
                    "state": info.get("state")})
    return out


# ---------- RPC getTokenSupply / getTokenLargestAccounts ----------
def rpc_supply(resp: dict) -> Optional[float]:
    """SOURCE: logs/raw/rpc_getTokenSupply.json result.value.amount (string, raw units)."""
    return _num(_get(resp, "result.value.amount"))


def rpc_largest(resp: dict) -> list:
    """Official Solana RPC shape result.value[].{address, amount}. The public RPC answered 429 when
    sampled (logs/raw/rpc_getTokenLargestAccounts_429.json); the Scanner falls back to RugCheck topHolders."""
    v = _get(resp, "result.value")
    return [{"address": a.get("address"), "amount": _num(a.get("amount")) or 0.0}
            for a in (v or []) if isinstance(a, dict)]


def delegations(accts: list) -> dict:
    """Group holder accounts by delegate -> {delegate: n_accounts}."""
    out = {}
    for a in accts:
        if a.get("delegate"):
            out[a["delegate"]] = out.get(a["delegate"], 0) + 1
    return out
