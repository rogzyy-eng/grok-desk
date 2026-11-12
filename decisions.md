# Desk decisions (Osman)
- STEP 1: fomo_handle = rogzyy3 (signed up via ref link)
- STEP 2: Jev skipped -> analyst_mode=rules. Telegram skipped -> notifier=webpush (iPhone home-screen app). Requires plan A hosting.
- Token-2022 program id in prompt is wrong; use TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb.
- Layer 8 proof PASSED on public RPC (CYBRON: Token 0, Token-2022 462 accounts, 138 holders).
- Layer 7: RugCheck insiders graph = list of networks {net_id, network_type, nodes[{id,participant,holdings}], links[{source,target}], relatedMint}; no 'edges' key. Fetch graph again when pool age >= 15 min; 200 [] => "None" (0 bundled); HTTP error/timeout => red "no data: bundles". Bundled = biggest network counting nodes with holdings>0.
- PumpPortal 'pool' = "pump-amm" string; real pool = DexScreener pairAddress / RugCheck markets[].pubkey. Creator null in migration -> RugCheck 'creator'.
- LP burned key not present; layer 5 uses markets[].lp.lpLockedPct.
- User plans to fund FOMO with ~$10 only after PAPER night; may lower min_ticket_usd to $5 then (ask again).

## VPS (2026-11-12)
- Droplet grok-desk created by user via panel (API create was 403 not authorized). id 618277419, fra1, s-1vcpu-1gb ($6/mo), Ubuntu 24.04, public IPv4 167.172.98.187, password auth, tag Rogzyy3. Install via DO Web Console.
