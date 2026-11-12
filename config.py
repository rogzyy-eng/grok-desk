"""config.py — environment + STEP 10 settings defaults. Secrets come ONLY from the environment
(systemd EnvironmentFile /etc/grok-desk/desk.env, root-only)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DESK_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("DESK_DB", DATA_DIR / "desk.db"))
LOG_DIR = Path(os.environ.get("DESK_LOG_DIR", DATA_DIR / "logs"))
WEB_DIR = ROOT / "web"
RAW_DIR = ROOT / "logs" / "raw"

APP_TOKEN = os.environ.get("APP_TOKEN", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUB = os.environ.get("VAPID_SUB", "mailto:desk@localhost")
TG_TOKEN = os.environ.get("TG_TOKEN", "")            # skipped in STEP 2
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")   # Jev — skipped in STEP 2
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
HOST = os.environ.get("DESK_HOST", "127.0.0.1")
PORT = int(os.environ.get("DESK_PORT", "8787"))

SECRET_ENV_NAMES = ("APP_TOKEN", "VAPID_PRIVATE_KEY", "TG_TOKEN", "TYPESAFE_API_KEY")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# decisions.md: the prompt's Token-2022 id is invalid (RPC -32602 WrongSize); this is the real one.
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_GRAPH = "https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
LAYER8_PROOF_MINT = "A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump"   # CYBRON (STEP 2 sample)

DEFAULTS = {
    "scanner": {"min_liquidity_usd": 40000, "max_top10_pct": 25, "max_h1_change_pct": 80, "poll_s": 60,
                "min_pool_age_min": 10, "max_wait_min": 60, "runner_h24_pct": 300},
    "auditor": {"enabled": True, "max_dev_pct": 5, "max_top10_pct": 25, "min_lp_locked_pct": 99,
                "min_pool_age_min": 10, "max_bundled": 5, "max_delegated_accounts": 10, "retries": 3,
                "retry_gap_s": 2, "graph_min_age_min": 15},
    "analyst": {"mode": "rules", "alive_min": 0.70, "organic_min": 0.65, "chase_max": 0.30, "score_min": 70,
                "timeout_s": 10, "state_max_chars": 3000,
                "rules_alive_volume_h1": 25000, "rules_organic_buys_h1": 150, "rules_bundled_penalty": 0.5,
                "rules_bundled_from": 3, "rules_flush_pct": 5, "rules_chase_h1_pct": 80},
    "executor": {"stack_pct": 8, "max_open": 2, "min_ticket_usd": 10.0, "below_min": "bump",
                 "recheck_liq_drop_pct": 20, "recheck_price_move_pct": 15, "confirm_min": 5,
                 "max_liq_pct": 2},
    "risk": {"poll_s": 15, "stop_pct": 6, "trail_start_pct": 20, "trail_pct": 12, "timeout_h": 3,
             "timeout_min_gain_pct": 5, "sold_wait_min": 2, "no_price_polls": 4, "backoff_429_s": 30},
    "desk": {"mode": "PAPER", "paper_start_stack": 200, "report_time": "08:00", "notifier": "webpush",
             "rpc_url": "https://api.mainnet-beta.solana.com", "push_rejects": False,
             "fees_ref_discount": 0.10, "fomo_handle": "rogzyy3", "tz": "Europe/Istanbul"},
}

# Slider ranges for the Account tab (min, max, step). Not in the spec text; UI only.
RANGES = {
    "scanner.min_liquidity_usd": (1000, 200000, 500), "scanner.max_top10_pct": (5, 60, 1),
    "scanner.max_h1_change_pct": (10, 300, 5), "scanner.poll_s": (15, 300, 5),
    "scanner.min_pool_age_min": (0, 60, 1), "scanner.max_wait_min": (10, 240, 5),
    "scanner.runner_h24_pct": (50, 2000, 50), "auditor.max_dev_pct": (0, 20, 0.5),
    "auditor.max_top10_pct": (5, 60, 1), "auditor.min_lp_locked_pct": (50, 100, 1),
    "auditor.min_pool_age_min": (0, 60, 1), "auditor.max_bundled": (1, 30, 1),
    "auditor.max_delegated_accounts": (1, 100, 1), "auditor.retries": (1, 10, 1),
    "auditor.retry_gap_s": (0, 30, 1), "auditor.graph_min_age_min": (0, 60, 1),
    "analyst.alive_min": (0, 1, 0.01), "analyst.organic_min": (0, 1, 0.01), "analyst.chase_max": (0, 1, 0.01),
    "analyst.score_min": (0, 100, 1), "analyst.timeout_s": (2, 60, 1), "analyst.state_max_chars": (500, 3000, 100),
    "analyst.rules_alive_volume_h1": (1000, 200000, 1000), "analyst.rules_organic_buys_h1": (10, 1000, 10),
    "analyst.rules_bundled_penalty": (0, 1, 0.05), "analyst.rules_bundled_from": (1, 20, 1),
    "analyst.rules_flush_pct": (1, 50, 1), "analyst.rules_chase_h1_pct": (10, 300, 5),
    "executor.stack_pct": (1, 50, 0.5), "executor.max_open": (1, 10, 1), "executor.min_ticket_usd": (5, 500, 0.5),
    "executor.recheck_liq_drop_pct": (1, 80, 1), "executor.recheck_price_move_pct": (1, 80, 1),
    "executor.confirm_min": (1, 60, 1), "executor.max_liq_pct": (0.1, 10, 0.1),
    "risk.poll_s": (5, 120, 1), "risk.stop_pct": (1, 50, 0.5), "risk.trail_start_pct": (1, 200, 1),
    "risk.trail_pct": (1, 50, 0.5), "risk.timeout_h": (0.5, 48, 0.5), "risk.timeout_min_gain_pct": (0, 100, 1),
    "risk.sold_wait_min": (1, 30, 1), "risk.no_price_polls": (1, 20, 1), "risk.backoff_429_s": (5, 300, 5),
    "desk.paper_start_stack": (10, 10000, 10), "desk.fees_ref_discount": (0, 0.5, 0.01),
}

# "set stop to -8%" aliases (STEP 9)
ALIASES = {"stop": "risk.stop_pct", "trail": "risk.trail_pct", "trail start": "risk.trail_start_pct",
           "timeout": "risk.timeout_h", "stack pct": "executor.stack_pct", "size": "executor.stack_pct",
           "min ticket": "executor.min_ticket_usd", "max open": "executor.max_open",
           "min liq": "scanner.min_liquidity_usd", "liquidity": "scanner.min_liquidity_usd",
           "top10": "scanner.max_top10_pct", "score": "analyst.score_min", "alive": "analyst.alive_min",
           "organic": "analyst.organic_min", "chase": "analyst.chase_max", "dev": "auditor.max_dev_pct",
           "bundled": "auditor.max_bundled", "report": "desk.report_time", "notifier": "desk.notifier"}

AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")


def min_stack(min_ticket: float, stack_pct: float) -> float:
    return round(min_ticket / (stack_pct / 100.0), 2)
