# Osman's MASTER PROMPT, verbatim (parts 1-5)

MASTER PROMPT · PART 1 of 5
I'm sending you 1 prompt in 5 parts. Keep every part. Don't start any work until
PART 5 tells you to. After each part reply only: "got part 1, send the next".

===== GROK BOT BUILD PROMPT =====
You are building me a private trading desk: 5 agents running on your cloud computer, an iPhone
web app, and a Telegram bot. The agents find, audit, judge, size and set exits. I tap 1 button
on FOMO to trade. FOMO has no public order API: you never place orders, never ask for my FOMO
password, never touch my funds. Work step by step. After each step show proof and wait for "ok".

HELP MODE (every step, every question you ask me)
Before you ask me for anything (an account, a key, a token, a setting, an iPhone action), ask once:
"Do you know how to do this, or should I walk you through it?"
If I say no, or what I send back looks wrong: give me a numbered guide, 1 action per line:
where to go, what to tap, what I should see, what exactly to copy, what never to share.
Wait, then check it worked (a test call, a test message, a screenshot I send) before the next step.
Only describe screens you can actually open or that the official docs show. If you can't see it,
say so and tell me what to look for instead of guessing.
Have these guides ready: FOMO signup through the link + where my username is, a Jev key at
console.typesafe.ai (Keys, create, copy it at once, it's shown 1 time), a Telegram bot with
@BotFather (/newbot, a name, a username ending in "bot", copy the token, then send /start to it),
adding the app to the iPhone home screen (Safari, Share, Add to Home Screen) and allowing
notifications, saving a login in your Vault, switching PAPER to LIVE.

STEP 1 · FOMO ACCOUNT (blocking)
Ask: "Did you create your FOMO account through https://fomo.family/r/savipww ?"
FOMO applies a referral code only on the signup screen, there is no field for it later.
Tell me why it matters before I sign up: through that link every trading fee is 10% off for the life
of t...test_links.py test_gate.py test_risk.py test_executor.py test_replay.py
  logs/              1 file per agent, rotated daily, logs/raw/ samples
In STEP 8 and the agent prompts, {stop_pct} {trail_pct} {stack_pct} {timeout_h} are live
settings. A position shows the stop it carries (the setting at open, or a tighter one later).

STEP 4 · DATA MODEL (SQLite)
coins      (mint PK, name, symbol, pool, migrated_at, creator, creator_balance_at_migration, holders)
scans      (id, mint, ts, liquidity_usd, top10_pct, h1_change_pct, age_min, verdict, reason)
audits     (id, mint, ts, layer, name, value_text, pass, source)
verdicts   (id, mint, ts, alive_6h, organic, flush, chase, score, pass, reason, input_tokens)
signals    (id, mint, ts, price, size_usd, stack_pct, link, mode, status)
positions  (id, mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,
            closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode)
activity   (id, ts, kind bought|sold|stopped|rejected, mint, agent, title, subtitle, value)
prices     (mint, ts, price, volume_m5)      every poll, feeds the candles
balance    (ts, value)                       every minute, feeds the Home chart
settings   (key PK, value, updated_at)       change_log (ts, key, old, new, who)
skips      (mint, until)                     heartbeats (agent, ts)
push_subs  (endpoint PK, p256dh, auth, created_at)   web push subscriptions
Who writes db.prices (1 DexScreener batch per writer, never a 2nd source):
  Scanner  every [60]s, every coin in the waiting room
  Risk     every [15]s, every open position
  api.py   every [60]s, every coin pushed, audited or opened in the app tonight that neither
           of the two above covers, so every Coin screen has candles

STEP 5 · AGENTS
Implement the 5 agent prompts under this one, 1 module each, exactly as written.
Numbers in [brackets] are settings with those defaults. Agents talk only through bus.py and db.

END OF PART 1 of 5. Wait for the next part.

---

MASTER PROMPT · PART 2 of 5
Continue of the same prompt. Don't start yet. Reply only: "got part 2, send the next".

STEP 6 · LINKS (non-negotiable)
links.py:
  CHAIN_ID = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
  SIGNUP_URL = "https://fomo.family/r/savipww"
  def coin_url(address, chain="solana"):
      if chain not in CHAIN_ID: log it, return SIGNUP_URL
      return f"https://fomo.family/coin?address={address}&chainId={CHAIN_ID[chain]}&r=savipww&source=share_link"
  def profile_url(handle):
      h = handle.lstrip("@")
      return f"https://fomo.family/profile/{h}?source=share_link&r=savipww"
Every FOMO link the desk ever produces goes through these: Buy and Sell buttons, every
push on EVERY channel (Telegram, web push, Discord, email or whatever I picked in STEP 2),
Activity rows, Coin screen, Auditor screen, Account tab, morning Page, chat answers in Telegram
and in the Grok Bot app. pumpfun coins are Solana = 1399811149.
notifier.py takes the URL from links.py and puts it on the button. A channel without buttons
(email, plain text) gets the full links.py URL as a line under the message. A channel that
can't carry the full URL is not allowed: tell me and pick another.
No FOMO URL is ever written anywhere else in the code. Never drop r=savipww.
Never shorten or wrap a FOMO link: no t.co, no bit.ly, no redirect page, no tracking proxy.
The URL in every button is the exact string links.py returned.

STEP 7 · API (plan A; plan B writes the same shapes as JSON files)
Every call needs APP_TOKEN, else 401.
GET  /api/state                    balance, today %, multiplier, mode, 5 agent cards
GET  /api/chart?range=LIVE|1H|4H|1D|1W|ALL   line points + marks
GET  /api/activity?before=         rows, 30 per page
GET  /api/coin/{mint}              price, candles, stats, position, exit lines, coin_url
GET  /api/audit/{mint}             8 layers with values
GET  /api/agents/{name}/decisions  last 20 decisions
GET  /api/account                  mode, ....T2a"
  rejected: "Rejected · dev holds 31%" + "Grok Bot won't buy X"
  cleared:  "Cleared · 8/8 parts" + "Sent to Executor · $N"
  toggle "Take apart before buying" = settings.auditor.enabled, default on, turning it off asks twice
  bottom button "Open on FOMO" = links.coin_url

COIN SCREEN
  name, "X · Solana · 11h old", shield chip "8/8"
  big price, "▲ x% since entry"
  live candles with volume: 1-minute OHLC built from db.prices (we have no other candle source),
  volume bar = change of DexScreener volume.m5
  orange #ff9f0a dashed "Risk trailing stop" line at the current exit line
  Market cap / Liquidity / 24h volume (DexScreener) / Holders (counted by Auditor layer 8, "n/a" if none)
  "Your position · $X in · Executor", exit card: stop line, trail line, time left to timeout
  full-width button: "Buy on FOMO" (no position) or "Sell on FOMO" (open position) = links.coin_url

ACCOUNT TAB
  mode switch PAPER / LIVE (LIVE: type LIVE to confirm, rule in STEP 11), stack field
  "Your FOMO profile" row = profile_url(settings.fomo_handle)
  "Invite a friend" row = SIGNUP_URL through the iOS share sheet (navigator.share,
  fallback: copy to clipboard + "Link copied")
  settings: every STEP 10 key, grouped per agent, slider + exact number field
  Telegram: connected / not, "Send test push"

PUSH (Telegram; plus web push only if I add the app to my home screen and allow it)
  header "Grok Bot · now", coin logo, 1 line: "Sold X · +$N (+P%)" or "X hit the -{stop_pct}% stop · -$N"
  every push has its FOMO button from links.py
  web push: Account tab "Enable notifications" (shown only when opened from the home screen)
  registers sw.js, subscribes with the VAPID public key, POSTs the subscription to /api/push
  (plan B has no endpoint to POST to: no web push there, Telegram only, and the row says so).
  webpush.py sends to db.push_subs; tapping the push opens links.coin_url for that coin.
  410 Gone = delete that subscription.

END OF PART 2 of 5. Wait for the next part.

---

MASTER PROMPT · PART 3 of 5
Continue of the same prompt. Don't start yet. Reply only: "got part 3, send the next".

STEP 9 · TELEGRAM AND TALKING TO THE DESK
(No Telegram: the same pushes, buttons and commands go through the notifier from STEP 2.)
Long polling, owner only. Pushes exactly as the agent prompts say, PAPER pushes start with "PAPER · ".
Commands, as /command or plain text. The same works when I write to you in the Grok Bot app:
you read the db on your computer and call the API.
  buy X                   X pushed tonight: resend that push with [Buy on FOMO]. X never pushed:
                          run Auditor on it now, reply with the 8 layers + [Buy on FOMO] =
                          coin_url, and say "not sized, not on the desk". No order is placed.
  sell X                  [Sell on FOMO] for my open X position
  why X                   which agent stopped X, the layer or rule, the value, the time
  pause Scanner           pause or resume any agent (Risk not while a position is open)
  set stop to -8%         any setting by name or alias, reply "stop -6% -> -8%"
  stack 1240              LIVE stack
  status                  mode, balance, open positions, each with [Sell on FOMO]
  paper | live            switch mode. live needs me to type LIVE
  invite                  SIGNUP_URL
  report                  link to the last morning Page
X can be a name, symbol or mint. Name or symbol unknown to the db: ask me for the mint.
If it matches 2+ coins, ask which, each with its coin_url.
Unknown text: show this list. Every coin you mention in any reply carries its coin_url.

STEP 10 · SETTINGS (all editable in the app, Account tab, sliders + exact number field)
scanner  min_liquidity_usd 40000 · max_top10_pct 25 · max_h1_change_pct 80 · poll_s 60
         min_pool_age_min 10 · max_wait_min 60 · runner_h24_pct 300
auditor  enabled true · max_dev_pct 5 · max_top10_pct 25 · min_lp_locked_pct 99
         min_pool_age_min 10 · max_bundled 5 · max_delegated_acc...nt tab and the Page.
  Every FOMO URL contains r=savipww (SIGNUP_URL has it as /r/savipww). coin_url on an unknown
  chain returns SIGNUP_URL. No shortened or redirected FOMO URL anywhere. Fail = no deploy.
1b test_notify: send 1 PAPER push through every channel I set up. Each one must carry the
  exact links.py URL with r=savipww. Fail = no deploy.
2 test_gate: alive 0.69 / organic 0.64 / chase 0.31 / score 69 each reject; all at threshold pass.
  Same test in rules mode with fake DexScreener numbers, rows read "Analyst (rules)".
3 test_risk: +25% then 13% off the top = trail exit; -6.1% = stop; 3h at +4% = timeout;
  loosening the stop setting never moves an open stop down.
  "set stop to -8%", open a new PAPER position, drop it 8.1%: push, Activity row and mark read
  "-8%", the Risk pill reads "stop -8%"; a position opened at -6% still shows -6%.
4 test_executor: stack $200 -> $16 -> under $47.50 -> bump (default) sends $47.50, skip skips;
  PAPER start at $200 with skip -> the "every coin will be skipped" warning;
  pool liquidity $40,000 caps any size at $800.
5 Auditor on a mint with freeze authority set: red at layer 2, layers 3-8 "not checked".
6 test_replay: run 2 hours of live migrations through PAPER, show me the Activity.
7 Secrets: grep web/, logs/, the repo, API responses, the plan B JSON files and the Page for
  TYPESAFE_API_KEY, TG_TOKEN and VAPID_PRIVATE_KEY values: must be empty. Same grep for the
  APP_TOKEN value: empty too (it lives only in the #t= fragment and my localStorage).
  Then open {URL}#t={APP_TOKEN} on my iPhone: /api/state returns 200, without the token 401.
  Plan B: /d/ and any path without DATA_PATH return 404, the repo has no DATA_PATH in it.
8 Screenshots at 390px wide: Home, Agents tab, Auditor screen, Coin screen, Account tab,
  plus each screen's empty and error state. Send them to me.
Done = all 8 pass + the app URL opened on my iPhone + my first PAPER push through my notifier.

END OF PART 3 of 5. Wait for the next part.

---

MASTER PROMPT · PART 4 of 5
Continue of the same prompt. Don't start yet. Reply only: "got part 4, send the next".

===== AGENT 1 · SCANNER =====
You are Scanner, agent 1 of 5 on my trading desk. You count and you cut.
Mine, not yours: every number in [brackets] is my setting. You never change one.

OWN
Every coin that graduates off the pumpfun curve and lands on PumpSwap. Nothing else.

INPUT
- PumpPortal websocket wss://pumpportal.fun/api/data. ONE connection for the whole desk.
  On open send {"method":"subscribeMigration"}. Each message = 1 graduated coin.
  Free. Do not subscribe to trade-level streams (they cost SOL and we don't need them).
  Read mint, pool, signature and creator through fields.py (mapped from the raw sample in
  STEP 2). A key that isn't in the message stays null.
- DexScreener GET https://api.dexscreener.com/tokens/v1/solana/{mint1,mint2,...}
  no key. Use the PumpSwap pair (dexId pumpswap); if several pairs, the one with the most liquidity.
  Read: pairAddress, priceUsd, liquidity.usd, marketCap, volume, txns, priceChange.h1, pairCreatedAt.
- Solana RPC (settings.rpc_url): getTokenSupply, getTokenLargestAccounts.

DO, per coin
1. Save it to the waiting room with migratedAt. Creator: from the migration message; if null
   there, from the RugCheck report (key from fields.py). Store creator + creator balance NOW
   (Auditor needs it for "sold 0"). Creator still unknown: store null, Auditor turns layer 3 red.
2. Every [60]s re-read DexScreener for the whole waiting room in batched calls.
   Write each priceUsd + volume.m5 to db.prices (the Coin screen candles start here).
3. Kill on the first hit, in this order:
   liquidity.usd < [$40,000]                                  reason "liq $31.2K"
   top 10 holders > [25%] of supply, pool and curve excluded  reason "top 10 hold 38%"
   priceChange.h1 > [+80%]                                    reason "+112% in 1h"
   name or symbol copies a coin that ran today                reason "copycat of X"...    red "pool too young"
7 Bundled wallets   biggest funded-together group < [5] (MAPPING above)
                    ok "None"               red "14 bundled wallets"
8 Delegations       every token account of this mint (getProgramAccounts on Token AND
                    Token-2022, filter by mint, jsonParsed), group by delegate.
                    No single delegate over [10]+ holder accounts.
                    ok "None"               red "41 wallets → 9fQx...T2a" (drain found)
                    Also save holders = accounts with balance > 0 (the Coin screen shows it).

OUTPUT
{"agent":"auditor","mint":"<base58>","ts":"<ISO UTC>","passed":<bool>,"layersChecked":<1-8>,
 "layers":[{"n":1,"name":"Mint authority","pass":<bool>,"value":"<ui string>","source":"rpc|rugcheck"}],
 "holders":<int|null>,"reason":"<first red value|null>"}
passed -> bus.analyst. red -> Activity "Rejected X / Auditor · {reason} / Skipped".

GOOD
- Emit each layer the moment it finishes (SSE "audit_layer"). The disc animation in the app
  reads them live: cyan beam on the layer, then green check or red.
- Card: "{N} taken apart", 4x2 layer strips, "taking apart..." while running,
  then "8/8 → Executor" or the reason, "{N} rejected".

STOP
- 7 of 8 is a no. Never "mostly clean", never a warning instead of a red.
- Never skips a layer because a source is slow.
- Never re-audits a rejected mint the same night.

EDGE CASES
- Source error or timeout: retry [3] times, [2]s apart. Still nothing = red "no data: {layer}".
  No data is never a pass.
- getProgramAccounts is proven in STEP 2 before any night. If the RPC starts refusing it
  mid-night: red "no data: delegations", Telegram me at once with "layer 8 down, every coin
  is red until rpc_url changes", and put the count of coins it killed on top of the morning Page.
- Supply 0 or unreadable: red at layer 3, "no supply".

LOG  logs/auditor.log: mint, each layer's raw value + ms taken, final verdict.

END OF PART 4 of 5. Wait for the next part.

---

MASTER PROMPT · PART 5 of 5 (last)

===== AGENT 3 · ANALYST =====
You are Analyst, agent 3 of 5. You never decide yourself. You build a state, ask Jev
4 closed questions, and code applies the gate. Mine, not yours.

OWN    the score 0-100 and the pass or reject on entry quality
INPUT  Scanner JSON + Auditor JSON (all 8 values) + a fresh DexScreener read of the pool:
       priceUsd, liquidity.usd, marketCap, volume m5/h1, txns m5/h1 (buys, sells), priceChange m5/h1

STATE  plain text, numbers only, no opinions, max [3,000] characters:
"Coin {name} on Solana, graduated {age} min ago. Liquidity ${liq}. Market cap ${mcap}.
 Last 5 min: {b5} buys, {s5} sells, volume ${v5}, price {p5}%.
 Last 1h: {b1} buys, {s1} sells, volume ${v1}, price {p1}%.
 Top 10 hold {top10}%. Dev holds {dev}%, sold 0. LP {lp}. Bundled {bund}. Delegations {del}."

CALL
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer {TYPESAFE_API_KEY}
{"model":"jev-latest",
 "state":"<STATE>",
 "questions":{
  "alive_6h":{"type":"noul","instructions":"This coin is still actively traded 6 hours from now"},
  "organic": {"type":"noul","instructions":"Most buyers in the last 30 minutes are separate real wallets"},
  "flush":   {"type":"noul","instructions":"Price just flushed down and buyers are absorbing it"},
  "chase":   {"type":"noul","instructions":"This entry is chasing a green candle that already ran"}}}
Read answers.{name}.noul (0 to 1). Save "model" and "usage" from the response.

GATE (code only, Jev never sees it)
  alive_6h >= [0.70]
  organic  >= [0.65]
  chase    <= [0.30]
  score = round(100 * (alive_6h + organic + flush + (1 - chase)) / 4)
  score >= [70] -> bus.executor

OUTPUT
{"agent":"analyst","mint":"<base58>","answers":{"alive_6h":<0-1>,"organic":<0-1>,"flush":<0-1>,"chase":<0-1>},
 "score":<0-100>,"passed":<bool>,"failed":["<rule> <value>"],"model":"<from response>","inputTokens":<int>}
reject -> Activity "Rejected X / Analyst · {first failed rule} / Skipped"

GOOD
- Card: "S...ey
  exit price = the polled price that fired it, even if it gapped past the line

EXIT
  PAPER: close at the exit price, PnL minus fees.py on both sides
  LIVE:  push, position stays "exiting" until I tap [Sold]. No tap in [2] min: push again once, "still open"

PUSH   [Sell on FOMO] -> links.coin_url(mint, "solana") on every one, plus [Sold] (callback)
  trail:   "Sold {name} · +${pnl} (+{pct}%)"
  stop:    "{name} hit the -{stop_pct}% stop · -${loss}"      ({stop_pct} = this position's stop)
  timeout: "Sell {name}, dead money · {pct}% in {timeout_h}h"

OUTPUT (1 JSON per exit, to db.positions, db.activity and the app)
{"agent":"risk","mint":"<base58>","name":"<str>","ts":"<ISO UTC>",
 "reason":"stop|trail|timeout|pulled","entryPrice":<usd>,"exitPrice":<usd>,"peakPrice":<usd>,
 "pnlUsd":<usd>,"pct":<pct>,"feesUsd":<usd>,"stopPct":<this position's stop>,
 "mode":"PAPER|LIVE","status":"exiting|closed","link":"<links.coin_url>"}
LIVE: status "exiting" until I tap [Sold], then "closed". PAPER: "closed" at once.

ACTIVITY
  Sold X    / Risk · trailing stop / +${N} / +{P}%
  Stopped X / Risk · -{stop_pct}% stop / -${N}

GOOD
- Card: "Guarding" while anything is open; last exit "Trailed out" or "Stopped";
  pills "stop -{stop_pct}%" red and "trail {trail_pct}%" green, read live from settings.
- Coin screen: draw the current exit line as the orange dashed "Risk trailing stop".
STOP
- Never moves a stop down. A new stop setting applies to new positions, and to open ones
  only if it is tighter.
- Never "give it a minute", never waits for a bounce.
- Can't be paused while a position is open.
EDGE CASES
- No price for [4] polls in a row: push "{name}: no price for 1 min" with [Sell on FOMO].
  Never invent a price.
- Liquidity goes to 0: exit "pulled" at the last price, push now.
- 429: back off [30]s, log the missed polls.
LOG  logs/risk.log, every poll per position: price, peak, stop line, trail line, action.

That was PART 5 of 5, the whole prompt. Now start at STEP 1.
