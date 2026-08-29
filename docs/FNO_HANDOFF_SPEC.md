# F&O backtest handoff — what the trading bot needs, and in what shape

**Audience:** the Backtesting Engine session (sibling project). This file is the
contract between it and the live bot for Decision 036's two derivative buckets,
`futures-indian` and `options-indian`.

**Why it is this specific:** every field below is consumed by real code in
`D:\Claude_TVconnect2\trading-bot`, and the file that consumes it is named. A
field you invent will be ignored; a field you omit leaves a feature off. Nothing
here is a wish list.

**The one rule that matters more than the rest:** if you do not have a number,
**leave it blank**. Do not approximate, do not carry one over from an equity
run, do not reason one out. This repo's precedent is explicit — swing-indian's
`win_rate` baseline sat empty for months because a guessed baseline is worse
than none. A missing field costs a feature. A wrong field costs money.

---

## Deliver three files

Put them in the backtest's own results folder and give the bot session the path.

| File | What it is |
|---|---|
| `handoff.yaml` | Machine-readable parameters. Schema below. |
| `RULES.md` | Entry and exit logic, precise enough to reimplement without the backtest code. |
| `trades.json` | Every trade from the validated fold, for the parity harness. |

`trades.json` is not optional and not a formality. swing-indian went live only
after a parity harness reproduced **208 of its 214** backtested trades from the
live code path, and the six misses were explained (an EMA warm-up boundary)
rather than waved through. That harness is how a port is proven not to have
silently changed the strategy. Same bar here.

---

## 1. `handoff.yaml`

### 1a. Identity

```yaml
strategy_name: nifty_short_strangle     # becomes the .py filename and the
                                        # strategy_master.csv row; lowercase,
                                        # underscores, no spaces
bucket: options-indian                  # options-indian | futures-indian
tf: 1d                                   # the bar the SIGNAL is computed on
backtest_ref: >-                        # path to the run, so a wrong number is
  strategies/optimized/nifty_short_strangle/results/...json
fold: train                              # train | holdout | full — and EVERY
                                         # number below must come from THIS one
```

> **One fold, not a mix.** swing-indian's baselines were once a blend of train
> and full-run figures and it took a re-derivation to notice. State the fold and
> take every statistic from it.

### 1b. Sizing statistics — read the denominator note first

```yaml
sizing:
  # THE MOST IMPORTANT FIELD IN THIS FILE.
  # In F&O "notional" is ambiguous and the readings differ by ~100x: a NIFTY
  # option lot is ~Rs 13,000 of premium against ~Rs 15.8 LAKH of underlying.
  # Kelly must size against what the bucket actually risks.
  #   premium            -> long options; the premium IS the entire loss
  #   margin             -> futures and short options; margin is what is blocked
  #   underlying_notional-> only if you truly computed returns that way
  return_denominator: margin

  mu_per_period: 0.0074        # mean return per period, on the denominator above
  sigma_per_period: 0.0316     # std dev per period, same denominator
  period: 1d                   # what "per period" means. Usually the HOLDING
                               # period unit, not the signal tf — swing-indian
                               # signals on 1h and sizes per DAY

  # REQUIRED IN ADDITION TO mu/sigma, and not derivable from them.
  # Kelly's mu/sigma^2 form assumes a roughly bell-shaped return. A long option
  # is not: mostly -100%, occasionally a large winner. On that shape the
  # discrete form f* = (p*b - q)/b is the right estimator, and it needs p and b.
  win_rate: 0.62               # fraction of trades that won
  avg_win: 0.0412              # mean return of winners, on the denominator
  avg_loss: 0.0231             # mean return of losers, POSITIVE magnitude

  max_concurrent_positions: 4  # what the run actually held at once
  margin_per_lot_assumed_inr: 190000   # what the run assumed a lot blocks.
                                       # If reality is 13% and you assumed 10%,
                                       # the live book is smaller than validated
```

### 1c. Contract selection — this becomes `contracts.yaml` verbatim

Consumed by `src/shared/contract_selection.py`. Use these exact values.

```yaml
contract_selection:
  instrument: option           # option | future
  option_side: put             # call | put | directional
                               #   directional = long signal buys a call,
                               #   short signal buys a put
  strike_rule: otm_pct         # atm | otm_pct | itm_pct | otm_steps
  strike_value: 2.0            # percent for otm_pct/itm_pct; a COUNT of listed
                               # strikes for otm_steps; ignored by atm
  expiry_rule: weekly          # nearest | weekly | monthly
  min_days_to_expiry: 2        # the floor the run actually entered at
  max_days_to_expiry: 10       # or null
  min_open_interest: 0         # the run's liquidity filter, if any
  min_volume: 0
  signal_source: underlying    # underlying | contract — which series the
                               # signal was computed on
```

**`delta` is not an accepted `strike_rule`.** Nothing in the bot fetches
greeks, and a config asking for delta is rejected at load rather than silently
downgraded to ATM — a 0.30-delta strangle sized as if it were ATM is a
different trade. If your run selected by delta, say so in `RULES.md` and give
the closest rule above that reproduces it, plus how far apart the two are.

**A note on `weekly`:** NSE now lists weekly expiries for NIFTY only. On any
other underlying the bot falls back to the monthly. If your run assumed weeklies
on something else, that assumption is not reproducible live — flag it.

### 1d. Costs and fills

```yaml
execution:
  costs_modelled: true         # were costs in the backtest AT ALL?
  cost_note: >-
    Rs 20/order brokerage + 0.15% STT on the sell premium + 0.0355% exchange
    txn. State the rates you used; the bot has its own cited rate card and
    will reconcile against yours.
  fill_assumption: touch       # mid | touch | touch_plus_ticks
  fill_slippage_ticks: 0       # if touch_plus_ticks
  option_price_source: real_chain   # real_chain | black_scholes_synthetic
```

> **Say which price source honestly.** A run on synthesised option prices is a
> materially different confidence level from one on real historical chain data,
> and it should be labelled as one rather than quietly treated as fill-verified.
> Mid-fill on OTM stock options is fiction, and it flatters results most on
> exactly the trades that carry the edge.

### 1e. Baseline — reporting only, never read by the sizer

```yaml
backtest_baseline:
  profit_factor: 1.84
  win_rate: 0.62
  mean_trade_return: 0.0118    # per trade, on the same denominator as 1b
  trades: 96
  max_drawdown: 0.071          # as a fraction
  note: >-
    Which fold and why it is the right one to plan around.
```

### 1f. Universe and risk

```yaml
universe:
  underlyings: [NIFTY]         # explicit list. NSE only; BSE is out of scope
  # Or, for a stock-F&O run, the pinned list — the bot commits it to git so the
  # scanned set is auditable (House Rule 7).

risk:
  # For a SHORT position this is a PREMIUM MULTIPLE, not a price percent:
  # 100 = "close when the premium doubles", 200 = "when it triples".
  stop_loss_pct: 100
  # Underlying-level exit, if the run had one. Describe it in RULES.md too.
  underlying_stop: "spot breaches the short strike"
  exit_days_before_expiry: 2   # what the run actually did
```

> **The bot enforces a pre-expiry square-off regardless of what you send.**
> Stock derivatives are physically settled: an ITM contract carried past expiry
> delivers SHARES at full contract value — ~Rs 6.7 lakh for a median NSE lot
> against a Rs 5 lakh bucket. If your run held to expiry on stock F&O, its
> results are not reproducible live and we need to talk before anything ships.

---

## 2. `RULES.md`

Prose plus pseudocode, precise enough that someone reimplements the strategy
without reading the backtest code. It must cover:

1. **Entry condition** — every indicator, its exact parameters, and the bar it
   is evaluated on. "EMA20 on the continuous 1h close, fresh downward cross of
   -6.5%" is the standard to match, not "mean reversion signal".
2. **Which bar acts** — the close it triggers on, and when the order goes in.
3. **Exit conditions**, in priority order, including the time stop.
4. **Anything path-dependent** — warm-up bars, state carried between bars,
   look-back windows. This is where ports silently diverge.
5. **What was deliberately excluded** and why (a regime gate that was tested and
   removed is as important as one that was kept).

---

## 3. `trades.json`

One object per trade from the validated fold:

```json
[
  {
    "entry_time": "2026-03-04T09:15:00+05:30",
    "exit_time":  "2026-03-06T15:15:00+05:30",
    "underlying": "NIFTY",
    "contract":   "NIFTY-20260312-24500-PE",
    "expiry":     "2026-03-12",
    "strike":     24500,
    "option_type": "PE",
    "side":       "sell",
    "lots":       1,
    "lot_size":   65,
    "entry_price": 182.5,
    "exit_price":  96.0,
    "underlying_entry": 24610.2,
    "underlying_exit":  24788.0,
    "exit_reason": "premium_target",
    "pnl_inr":     5622.5,
    "costs_inr":   64.2,
    "margin_inr":  190000
  }
]
```

`contract` in that exact spelling — `<UNDERLYING>-<YYYYMMDD>-<STRIKE>-<CE|PE>`
for options, `<UNDERLYING>-<YYYYMMDD>-FUT` for futures. It is the bot's
canonical symbol (`src/shared/contracts.py`), and it uses the full expiry date
on purpose: Dhan's own contract name carries only the expiry MONTH, so five
different NIFTY weeklies share one string and keying on it trades the wrong
expiry.

`underlying_entry` / `underlying_exit` matter even when the signal is computed
on the option: they are what the parity harness re-derives strike selection
from.

---

## What happens on this side

1. `handoff.yaml` becomes `allocator.yaml`, `contracts.yaml`, `scanner.yaml` and
   a `strategy_master.csv` row in the bucket folder.
2. `RULES.md` becomes a `Strategy` subclass.
3. `trades.json` becomes a parity script under `scripts/`, and its pass rate is
   reported honestly — including the misses and their explanation.
4. Both buckets ship `enabled: false`. Going live is a separate, deliberate act.

Two gates are open on this side regardless of the handoff, and neither is
yours to clear:

- Dhan's margin preflight (`/v2/margincalculator`) has never been answered by a
  live account. No F&O order can be sized until it is.
- The fee rate card is unsigned pending an unexplained STT finding.

---

## Not supported yet — do not hand over a run that needs it

**Multi-leg structures.** Spreads, straddles and strangles need a position-group
model the bot does not have: how legs pair, which is the risk leg, what a
partial fill on one implies for the other. Naked shorts and single legs are
fully handled.

If the strategy is a **two-leg strangle**, say so up front — it changes the
build order, and shipping it as two independent single legs would be wrong in a
way that only shows up when one leg fills and the other does not.
