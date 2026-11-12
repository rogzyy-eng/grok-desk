# Field mapping (STEP 2 raw samples)

Mint used: `A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump` (CYBRON / CYBRON)  
Source: PumpPortal `subscribeMigration` (live), received 2026-11-11 23:13:41 Europe/Istanbul.  
Paths are exact JSON key paths from the saved raw samples. A field not present in the sample is `null` (not guessed).

## PumpPortal migration — `logs/raw/pumpportal_migration.json`

The saved body is a single object (not wrapped). Observed keys: `signature`, `mint`, `txType`, `pool`.

| Agent field | JSON path | Found | Sample value |
|---|---|---|---|
| mint | `mint` | yes | `A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump` |
| pool | `pool` | yes | `pump-amm` (string label, **not** a base58 pool address) |
| signature | `signature` | yes | `5Y65JvfzrbkorB1NBPk7C5tVGzdhxsywsBAMK2dPs7YEBwsTPopjNwti6vhjAsSuU6XWCWTiR1vbWHYYDUtmthER` |
| creator | — | **null** | key not present in the migration message |

Also observed, not requested: `txType` = `"migrate"`.

The on-chain PumpSwap pool address for this mint showed up later as DexScreener `[].pairAddress` and RugCheck `markets[].pubkey` where `marketType` = `"pump_fun_amm"` (`DUxG3Y9BAe6kvz2kkXYtHwRpc5jDj2pSh7xk9bYyhwwD`). That is **not** a PumpPortal field.

## DexScreener — `logs/raw/dexscreener_pair.json`

Endpoint: `GET https://api.dexscreener.com/tokens/v1/solana/{mint}`.  
Response is an **array of pair objects**. The saved sample is 1 pair with `dexId` = `"pumpswap"` (preferred). Paths below are relative to each element (`[].…`).

First fetch immediately after migration returned only a `dexId=pumpfun` pair and **no** `liquidity` key. A retry ~1 minute later returned the PumpSwap pair; that later body is what was saved.

| Agent field | JSON path | Found | Sample value (saved pumpswap pair) |
|---|---|---|---|
| pairAddress | `[].pairAddress` | yes | `DUxG3Y9BAe6kvz2kkXYtHwRpc5jDj2pSh7xk9bYyhwwD` |
| dexId | `[].dexId` | yes | `pumpswap` |
| priceUsd | `[].priceUsd` | yes | `"0.00001089"` (string) |
| liquidity.usd | `[].liquidity.usd` | yes | `6994.09` |
| marketCap | `[].marketCap` | yes | `10880` |
| volume.m5 | `[].volume.m5` | yes | `58540.68` |
| volume.h1 | `[].volume.h1` | yes | `58540.68` |
| volume.h24 | `[].volume.h24` | yes | `58540.68` |
| txns.m5.buys | `[].txns.m5.buys` | yes | `498` |
| txns.m5.sells | `[].txns.m5.sells` | yes | `536` |
| txns.h1.buys | `[].txns.h1.buys` | yes | `498` |
| txns.h1.sells | `[].txns.h1.sells` | yes | `536` |
| priceChange.m5 | `[].priceChange.m5` | yes | `-68.98` |
| priceChange.h1 | `[].priceChange.h1` | yes | `-68.98` |
| priceChange.h24 | `[].priceChange.h24` | yes | `-68.98` |
| pairCreatedAt | `[].pairCreatedAt` | yes | `1794428021000` (ms epoch) |

Also observed on the pair: `liquidity.base`, `liquidity.quote`, `volume.h6`, `txns.h6`, `priceChange.h6`, `fdv`, `priceNative`, `chainId`, `url`, `baseToken`, `quoteToken`.

## RugCheck report — `logs/raw/rugcheck_report.json`

Top-level object.

| Agent field | JSON path | Found | Sample value |
|---|---|---|---|
| creator | `creator` | yes | `GAHEsVuqnfv242J96FimQiobCYpZb1suDEALYRHqmPmF` |
| creator balance | `creatorBalance` | yes | `0` |
| markets[] pool address | `markets[].pubkey` | yes | `DUxG3Y9BAe6kvz2kkXYtHwRpc5jDj2pSh7xk9bYyhwwD` (marketType `pump_fun_amm`); also `GEoY7P6fKCMRCjLubcfAsipNXxF8JqZwZcmSLVBBnGjR` (marketType `meteora_damm_v2`). There is **no** `pool` key on a market. |
| LP locked percent | `markets[].lp.lpLockedPct` | yes | `100` (pump_fun_amm); `0` (meteora_damm_v2) |
| LP burned percent | — | **null** | no `burn` / `lpBurned` / `lpBurnedPct` key. Related (not used as a stand-in): `markets[].lp.lpLocked`, `lpUnlocked`, `lpCurrentSupply`, `currentSupply`, `totalTokensUnlocked` |
| topHolders | `topHolders` | yes | array of 20 objects; keys: `address`, `amount`, `decimals`, `pct`, `uiAmount`, `uiAmountString`, `owner`, `insider` |

Also observed and useful later: `mint`, `mintAuthority`, `freezeAuthority`, `token.supply`, `token.decimals`, `totalHolders`, `totalMarketLiquidity`, `launchpad`, `insiderNetworks` (null here), `graphInsidersDetected` (0).

## RugCheck insiders graph — `logs/raw/rugcheck_insiders_graph.json`

HTTP 200. Body is an empty JSON array: `[]`. No object keys were present, so node/edge shapes were not observed on this mint.

| Agent field | JSON path | Found | Sample value |
|---|---|---|---|
| nodes | — | **null** | not present (response is `[]`) |
| edges | — | **null** | not present (response is `[]`) |
| wallet key on a node | — | **null** | not observed |
| holding key on a node | — | **null** | not observed |
| funding-link keys on an edge | — | **null** | not observed |

## RPC getAccountInfo — `logs/raw/rpc_getAccountInfo.json`

Method: `getAccountInfo(mint, {encoding: "jsonParsed"})`. This mint is Token-2022 (`result.value.data.program` = `"spl-token-2022"`).

| Agent field | JSON path | Found | Sample value |
|---|---|---|---|
| mintAuthority | `result.value.data.parsed.info.mintAuthority` | yes | `null` (revoked) |
| freezeAuthority | `result.value.data.parsed.info.freezeAuthority` | yes | `null` (revoked) |
| supply | `result.value.data.parsed.info.supply` | yes | `"996998474752288"` (string) |
| decimals | `result.value.data.parsed.info.decimals` | yes | `6` |
| program owner | `result.value.owner` | yes | `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` |

Also observed: `result.value.data.program` = `"spl-token-2022"`; token-2022 `extensions` include `metadataPointer` and `tokenMetadata` (name/symbol `CYBRON`).

## Note on the Token-2022 program id

`getAccountInfo` owner (canonical Token-2022 program) is `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`.  
The id given in the master prompt, `TokenzQdBNbLqP5VEhdkAS5EPFLC1PEuX8ofVHFKKq3s`, is a different / invalid pubkey (`getProgramAccounts` returned `-32602 Invalid param: WrongSize`).
