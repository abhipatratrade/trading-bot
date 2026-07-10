# Interim Dhan Scanner — Blasting Momentum Swing v1 (3× MTF)

**Status: Phase-4 bridge tool.** The `swing-indian` bucket's strategy files live
in `src/strategies/swing/indian/` (scanner.yaml, allocator.yaml,
strategy_master.csv, strategies/blasting_momentum.py) but the bucket can't run
in the main loop yet — the Dhan data adapter + broker adapter (Phase 3,
Decision 012) don't exist. This standalone runner executes the same spec
against the Dhan **sandbox** (testnet) in the meantime. Retire it when
`swing-indian` flips to `enabled: true` in buckets.yaml.

> This tool is OUTSIDE the deterministic bot loop: no Postgres, no kill switch,
> no idempotent order ids, no audit rows. That is acceptable for the sandbox
> and dry-runs; do **not** point it at live money once Phase 4 lands.

## Strategy (v1, backtested)

At **09:45 IST**, scan the **entire tradeable NSE + BSE cash-equity universe**
(~4,000 symbols: NSE `EQ` series + BSE-only `A/B/X/XT`, ISIN-deduped preferring
NSE) for: gap-up ≥ 2% vs prev close · daily RSI(14) ≥ 65 **and rising** ·
EMA(10) > EMA(20) · CCI(14) ≥ 200 · 09:15→09:45 volume ≥ 20k · price ₹100–2000 ·
turnover ≥ ₹5L. Rank by gap %, **BUY top 5** (₹10k notional each,
`productType=MTF` ≈ 3× funding, CNC fallback). **Exit** on daily
Supertrend(10,3) flip below close, or after 30 days.

backtest_ref: `Backtesting Engine/results/learnings/2026-07-09_blasting_momentum_swing.md`
(+23.5% unlevered, PF 1.71, DD 7.3%; ≈ +54% at 3× MTF net of ~16% funding).
**The backtest universe was Nifty 500** — whole-market NSE+BSE microcaps are
untested; the price/volume/turnover floors are the only guardrails. Watch fill
quality and candidate counts before trusting live P&L.

## Setup

```bash
cd scripts/dhan-scanner
pip install -r requirements.txt
copy .env.example .env      # then edit: tokens, client ids
```

You need BOTH: the live `DHAN_ACCESS_TOKEN` (market data — the sandbox has no
data feed) and the sandbox token/client-id pair (orders) while `DHAN_ENV=sandbox`.

## Daily schedule (IST) — Windows Task Scheduler

| time | command | notes |
|---|---|---|
| 18:00 | `python scanner_live.py prepare` | ~4,600 symbols → shortlist; **~40–60 min** |
| 09:44 | `python scanner_live.py scan` | intraday confirm on shortlist only (seconds), BUY free slots |
| 15:15 | `python scanner_live.py manage` | Supertrend-flip / 30-day-cap SELLs on today's forming close |

`schtasks /create /tn DhanScanPrepare /tr "python D:\Claude_TVconnect2\trading-bot\scripts\dhan-scanner\scanner_live.py prepare" /sc weekly /d MON,TUE,WED,THU,FRI /st 18:00`
(repeat for scan @ 09:44 and manage @ 15:15).

**Run scan/manage with `--dry-run` for the first week** and compare picks with
the backtest scanner. `--refresh-universe` rebuilds the NSE+BSE symbol map
(do monthly, or after index/listing changes).

## Faithfulness to the backtest

* RSI/CCI/EMA/Supertrend are **imported from the Backtesting Engine**
  (`BACKTEST_ENGINE_DIR`) — same code that produced the backtest numbers.
* `prepare` = daily indicators as of prior close (no look-ahead);
  `scan` entry = the 09:45 15m bar open; `manage` evaluates the Supertrend flip
  on today's *forming* daily bar at 15:15 (backtest uses the settled close —
  a last-15-minutes divergence, expected and small).

## Known deviations / risks

1. **Universe mismatch**: backtest = Nifty 500; live = all NSE+BSE. Candidate
   counts will be higher and include microcaps. If live picks differ wildly
   from the backtest's character (avg candidate count, fill rate), stop and
   re-backtest on the wider universe before scaling.
2. **MTF eligibility**: broker-approved scrips only (BSE-only names rarely
   qualify). `MTF_FALLBACK_CNC=true` silently degrades those to 1× — check
   `state/trades.csv` (`product` column) to see the real leverage mix.
3. **MTF funding** (~14–18% p.a. on the funded ⅔) is invisible in the sandbox;
   the 3× expectation is ~+54%/yr net, NOT 3 × 23.5%.
4. **Margin calls at 3×** (~22% historical margin-DD) are not simulated.
5. Graduate deliberately: sandbox dry-run → sandbox orders → small live CNC 1×
   → MTF 3× — and Phase 4 should replace this tool entirely.
