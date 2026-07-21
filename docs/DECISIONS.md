# DECISIONS.md — Architecture Decision Log

Append-only. Never delete or rewrite a past decision. To change one, add a
new entry that supersedes it (cite the old number in `Supersedes:`).

Format:

```
## NNN — Title
Date: YYYY-MM-DD
Status: Accepted | Superseded by NNN
Supersedes: (optional)

Decision: one-paragraph statement.
Rationale: why this over alternatives.
Consequences: what this commits us to / rules out.
```

---

## 001 — Language: Python
Date: 2026-04-29
Status: Accepted

Decision: All bot code in Python ≥ 3.11.
Rationale: HMM ecosystem (`hmmlearn`), pandas, FastAPI, Kite SDK, Delta SDK
all native to Python. User already proficient. No reason to add a second runtime.
Consequences: Single Python toolchain end-to-end. Type hints required;
`pydantic` for config and DTOs.

---

## 002 — Hosting: Railway (not AWS)
Date: 2026-04-29
Status: Accepted

Decision: Host bot-worker, dashboard, scheduler, and Postgres on Railway.
Rationale: For solo 24/7 workload with ₹50k starting capital, Railway's
near-zero DevOps overhead beats AWS. AWS only wins at scale we won't reach.
Consequences: Accept Railway's per-service resource caps. Revisit if we
ever need multi-region or sub-100ms execution latency.

---

## 003 — Dashboard: FastAPI + HTMX (single Python service)
Date: 2026-04-29
Status: Accepted

Decision: Server-rendered HTML via FastAPI + HTMX. Not Next.js.
Rationale: Solo user, no SaaS aspirations. Halves Railway service count and
removes Node toolchain. Live updates via HTMX polling/SSE are sufficient.
Consequences: No SPA features. Can revisit if we ever expose to other users.

---

## 004 — Crypto signals from Binance, execution on Delta India
Date: 2026-04-29
Status: Accepted

Decision: Use Binance public WS/REST for OHLCV, OI, and signal generation.
Execute orders on Delta Exchange India only.
Rationale: Binance has highest crypto volume globally → best price discovery.
Delta India is the Indian-resident-friendly execution venue.
Consequences:
- Universe = intersection of (Delta India listed) and (Binance listed).
- Symbol mapping table required (`BTCUSDT` ↔ `BTCUSD`).
- For funding-aware logic, use **Delta's funding rate**, not Binance's.
- Position sizing reads **Delta's order book depth**, not Binance's.
- Drift monitor: log `delta_price - binance_price`; auto-pause symbol on
  threshold breach.
- Reconsider for Phase 5/6 (5–15M, 25–100x): India→Binance latency
  (~150–250ms) and price drift may exceed edge at those timeframes.

---

## 005 — Stocks: Zerodha Kite Connect for data + execution
Date: 2026-04-29
Status: Accepted

Decision: All Indian equity data and orders via Kite Connect.
Rationale: Only legal route for live NSE data and execution from India.
User already runs an existing stocks long-term system on Kite.
Consequences: Kite Connect subscription required (~₹2k/month). Existing
system is ported into this repo's `src/strategies/stocks_longterm/` in Phase 3.

---

## 006 — Strategy parameters: YAML in git, not DB, not dashboard-editable
Date: 2026-04-29
Status: Accepted

Decision: Each strategy has `policy.yaml` schema-validated on bot startup.
Every change committed to git, references a `backtest_ref` (run ID from
the separate backtester). An audit table `strategy_param_changes` records
every load with version + git SHA.
Rationale: Reversibility, diffability, and forced deliberation. Mutable
runtime config is how people lose money at 3am.
Consequences:
- Dashboard is read-only for params (display current values, never edit).
- Bad YAML refuses to start the bot — fail-fast over silent default.
- Param tuning by an LLM agent (later phase) opens a PR; user reviews, merges.

---

## 007 — Trade archive: Postgres truth + Google Drive nightly mirror
Date: 2026-04-29
Status: Accepted

Decision: Postgres on Railway is source of truth. Nightly job exports the
day's trades as Parquet + CSV to a Google Drive folder. User's Drive
desktop client mirrors to local automatically.
Rationale: Cloud-primary survives laptop loss; local mirror gives offline
access and backtester-friendly Parquet.
Consequences: `GDRIVE_*` env vars on scheduler service. Service account
or OAuth token required. CSV is for human review, Parquet for the
backtester to ingest.

---

## 008 — Architecture: deterministic core, agentic perimeter
Date: 2026-04-29
Status: Accepted

Decision: Trading loop is pure code (scanner → brain → allocator → safety
→ broker). LLM agents may be added later in a separate phase as advisory
tools (postmortem, research assistant, news classifier, param tuner) but
never in the order-decision path.
Rationale: Determinism for backtest parity, sub-second latency, low cost,
auditable decisions, regulatory defensibility.
Consequences: Agentic phase (Phase 7+) builds tools that read DB and write
reports / open PRs. They never touch order placement. Strategy code must
remain importable by the (separate) backtester unchanged.

---

## 009 — Backtest engine: out of scope for this repo
Date: 2026-04-29
Status: Accepted

Decision: User builds the backtester in a separate repo. This repo exposes
strategy logic (`src/scanner`, `src/allocator`, `src/safety`, strategy
runners) as importable modules so the backtester can consume them without
duplication.
Rationale: User preference; keeps live-trading repo small and focused.
Consequences: Strategy modules must avoid live-only imports at module
top-level (e.g., don't `import delta_client` at file scope). Use lazy
imports or dependency injection so a backtester can swap data + broker.

---

## 010 — Alerts: Telegram
Date: 2026-04-29
Status: Accepted

Decision: All breaker / anomaly / daily-summary alerts via Telegram bot.
Rationale: Free, reliable in India, mobile-first.
Consequences: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` env vars on
bot-worker and scheduler. Alert module no-ops when unset (so testnet
without Telegram still runs).

---

## 012 — Stocks broker: switch from Zerodha to Dhan
Date: 2026-05-03
Status: Accepted
Supersedes: 005

Decision: Use Dhan (DhanHQ API) instead of Zerodha Kite Connect for all
Indian equity data and execution (Phase 3 onwards).
Rationale: The Zerodha decision assumed an existing live Kite system —
that system does not exist. Dhan has a free API tier (vs ₹2k/month for
Kite Connect), access tokens valid for 30 days (vs Zerodha's daily expiry
which requires a login script), and user already has a Dhan account.
Consequences:
- Remove all Kite-related config, broker adapter, and data source code.
- Add Dhan REST + WebSocket broker adapter in Phase 3.
- BrokerName enum: replace ZERODHA with DHAN.
- Symbol mapping: use Dhan symbol format instead of Kite NSE tokens.
- No daily re-authentication cron job needed (30-day tokens).

---

## 011 — Session continuity: CLAUDE.md + PHASES.md + DECISIONS.md
Date: 2026-04-29
Status: Accepted

Decision: Three files persist context across Claude sessions. `CLAUDE.md`
is the bible pointer + locked-decisions quick ref + house rules.
`docs/PHASES.md` is the live build tracker with checkboxes.
`docs/DECISIONS.md` is this file (append-only).
Rationale: User wants to resume work in new sessions without re-briefing.
Consequences: Every session ends by ticking PHASES.md and committing.
Decisions never live in chat memory alone — they're written here first.

---

## 013 — Architecture: (type × market) buckets with isolated capital
Date: 2026-06-10
Status: Accepted

Decision: The system is partitioned into six independent buckets — one
per (trading_type × market) pair: longterm-crypto, swing-crypto,
scalp-crypto, gambling-crypto, longterm-indian, swing-indian. Each
bucket has a fixed ₹50,000 INR capital pool tracked in the
`bucket_state` Postgres table, its own folder under
`src/strategies/<type>/<market>/`, its own broker, leverage cap, and
configuration files (`scanner.yaml`, `regime.yaml`, `allocator.yaml`,
`strategy_master.csv`). All six pre-funded up-front (₹3,00,000 total).

Rationale: Matches the user's PPTX instructions
(`C:\Users\User\Documents\Trading bot instructions.pptx`, slides 4-5)
and Goal_Setting.txt portfolio percentages. Isolation between buckets
means a blowup in one strategy class can't drain another. Per-bucket
sizing simplifies Kelly accounting.

Consequences:
- Folder structure is `src/strategies/<type>/<market>/`.
- `buckets.yaml` at repo root is the single source of truth for
  capital + broker + leverage cap + enabled flag.
- `BucketState` table tracks live capital vs locked margin.
- Cross-bucket capital rebalancing is explicitly out of scope for v1.
- Indian buckets ship disabled until the Dhan adapter lands (Phase 3).

---

## 014 — Per-bucket regime HMM at the bucket's TF
Date: 2026-06-10
Status: Accepted

Decision: Each bucket gets its own Hidden Markov Model fit on a proxy
symbol (BTCUSDT for crypto; NIFTY-equivalent for Indian later) at the
bucket's own TF — 1D for longterm, 1H for swing, 5M for scalp/gambling.
Three hidden states mapped to `bear` / `neutral` / `bull` by sorting on
mean log return. Models live in the `regime_model` Postgres table
(JSONB serialised hmmlearn params, no pickle, no filesystem).

Rationale: PPTX slide 4(b) — regime TF matches strategy TF, not a single
global regime. Three states are robust to limited crypto history; 5
states (Crash/Euphoria) deferred until ≥ 3 months of live signal.

Consequences:
- Six retrain jobs (one per bucket), cadence per `regime.yaml`.
- Scheduler service wires APScheduler crons keyed by bucket id.
- `RegimeSnapshot` row written every inference; `REGIME_CHANGE` audit
  event when the label flips.
- 5-state model and Crash kill-switch coupling stay deferred.

---

## 015 — Kelly on bucket capital with insufficient-balance skip rule
Date: 2026-06-10
Status: Accepted

Decision: Position sizing uses continuous Kelly (`f* = μ/σ²`), scaled
by `fractional_kelly` (default 0.25) and the current regime's
multiplier. The notional is computed against `bucket.capital_inr` (not
aggregate equity). Per-symbol (μ, σ) come from the user's separate
backtester and are committed to `allocator.yaml` in each bucket folder
with a `backtest_ref` (per Decision 006).

If `suggested_notional > bucket.available_balance_inr`, the trade is
**skipped entirely** — no partial fill. Sized snapshot is written with
`decision = SKIPPED_INSUFFICIENT`.

Rationale: PPTX slide 4(d). Skipping under-sized trades preserves
Kelly's edge profile (partial fills change the variance of returns and
ruin the optimality). Backtester-fed stats keep param choices
auditable; live rolling estimation is deferred to Phase 7.

Consequences:
- New `SizingDecision` enum + `sizing_snapshot` audit table.
- Allocator is pure (no I/O); orchestration in `sizer.size_positions`.
- Long-only for v1: μ ≤ 0 → SKIPPED_NEGATIVE_EDGE (no shorting).
- Dedup gate inside the sizer: (bucket_id, strategy_name, symbol) open
  → SKIPPED_DEDUP.

---

## 016 — Strategy Master is a CSV per bucket (OR-semantics regime gate)
Date: 2026-06-10
Status: Accepted

Decision: Each bucket folder contains a `strategy_master.csv` file
(plain CSV, git-tracked) listing every strategy with columns
`strategy_name, tf, min_vol, trading_regime_1, trading_regime_2,
trading_type`. Loaded and pydantic-validated on bucket startup; fail-fast
on any schema/consistency violation.

Regime-gate semantics are **OR**: a strategy trades if the current
regime is in `{trading_regime_1, trading_regime_2}` (blanks ignored).
Both blank ⇒ strategy is regime-agnostic.

Rationale: PPTX slide 4(c) explicitly references "Excel header" but the
user picked CSV-in-git when asked: opens in Excel naturally,
git-diffable, no binary churn. OR-semantics match the natural reading
of "Trading Regime 1 or 2".

Consequences:
- `src/shared/strategy_master/{schema,loader}.py` implement validation.
- Strategy files are auto-discovered in `strategies/<type>/<market>/strategies/`
  (one `Strategy` subclass per `.py`); the master CSV gates them.
- AND / weighted variants stay out of scope for v1.

---

## 017 — Phase 1 soak restarts under the new bucket structure
Date: 2026-06-10
Status: Accepted

Decision: The in-flight 14-day testnet soak (started 2026-05-03 on the
GCP VM) is restarted from scratch after the bucket restructure. The old
`src/strategies/crypto_longterm/` is removed; the same logic is ported
as `src/strategies/longterm/crypto/strategies/top5_volume.py`. Existing
DB rows are backfilled by migration 0002 (`bucket_id='longterm-crypto',
strategy_name='top5_volume'`) so historical positions/trades carry the
new identifiers.

Rationale: User chose "Restructure immediately; restart soak" when
asked. Cleaner single structure, modest cost (~5 days of soak data
lost), avoids carrying two parallel runtime paths.

Consequences:
- The 14-day testnet soak clock resets at the next deploy.
- Live capital deployment (₹50k on `longterm-crypto`) follows the new
  soak window, not the original schedule.
- Backfill is a one-shot at migration time; no parallel-path support
  code lives in the codebase.

---

## 018 — Kelly insufficiency check uses required margin, not notional
Date: 2026-06-10
Status: Accepted
Clarifies: 015

Decision: The Kelly-vs-balance check in `shared.allocator.sizer` compares
**required margin** (not leveraged notional) against
`bucket.available_balance_inr`:

    required_margin = capital × kelly_used × regime_multiplier
    notional        = required_margin × leverage_max
    if required_margin > available → SKIPPED_INSUFFICIENT

Rationale: Decision 015's wording said "notional > available → skip",
but at typical Kelly weights (Kelly fraction × per-symbol cap ≈ 0.1-0.3)
combined with leverage ≥ 5x this would cancel every leveraged trade —
margin would be tiny while notional > capital is the *normal* case for
leveraged perps. The PPTX rule "less balance than Kelly suggests → do
not enter" reads more naturally as a margin check, since that is the
actual cash at risk per Kelly's edge calculation.

Consequences:
- `SizingSnapshot.suggested_notional_inr` continues to record the full
  leveraged notional (what the broker sees).
- `available_balance_inr` is now compared to margin, not notional.
- Backtester reproducibility: the same check (margin > available)
  must be used in the separate backtester repo.
- This is a behaviour change vs the previously-committed sizer; old
  testnet runs would have skipped most leveraged candidates. Phase 1
  exit criterion still requires 14 clean testnet days post-change.

---

## 019 — One Delta India sub-account per crypto bucket
Date: 2026-06-17
Status: Accepted
Refines: 013

Decision: Each crypto bucket trades on its **own Delta India sub-account**
(separate API key/secret), instead of all crypto buckets sharing one
account. A bucket's `account_ref` in `buckets.yaml` selects its credential
set (`DELTA_<REF>_<MODE>_API_KEY/_SECRET`); `account_ref: default` reuses the
original top-level keys.

Rationale: A perp exchange nets positions **per symbol per account**. With a
shared account, two buckets trading the same coin (e.g. BTC in longterm and
swing) collapse into one exchange position, which breaks Decision 013's
per-bucket isolation in three ways:
1. **Leverage collision (hard blocker):** leverage is per-symbol per-account,
   so the 5x/10x/25x/100x ladder is unsatisfiable on one account — the last
   `set_leverage` wins.
2. **Cross-bucket interference / shared liquidation:** opposing or
   same-direction signals net together; a 100x gambling position can
   liquidate the longterm position because they are one position.
3. **Reconciler can't attribute** a netted position to the owning bucket.

A sub-account per bucket maps "isolated capital" 1:1 onto "isolated account":
own netted position, own per-symbol leverage, own margin pool.

Scope / mechanics:
- Confined to the execution-wiring layer. `run_bot` builds one
  `DeltaIndiaClient` + `OrderManager` + `Reconciler` per distinct
  `account_ref` among enabled crypto buckets; `BucketRunner` resolves its
  broker/OM by `account_ref`. Strategy/scanner/regime/allocator logic is
  unchanged.
- **Reconciler scoping:** each per-account reconciler restricts its DB
  queries to `Position.bucket_id` / `Trade.bucket_id` in its account's
  bucket(s), so it never sweeps another bucket's rows. The cross-bucket
  orphan-attribution heuristic collapses (an orphan on a sub-account belongs
  to that bucket).
- **No DB migration:** `Trade`/`Position` already carry `bucket_id`; `broker`
  stays `DELTA_INDIA` for all sub-accounts (same exchange).
- Public market data (`DeltaIndiaData`, Binance) stays a single shared
  instance — only execution is per-account.

Consequences:
- The **current account becomes `longterm-crypto`'s sub-account** (`default`)
  — no disruption to the in-progress soak. New sub-accounts are provisioned
  per phase (swing P2, scalp P5, gamble P6), each whitelisting the VM IP and
  funded separately.
- `Settings.delta_account(ref)` fails fast at startup if an enabled bucket's
  credentials are missing for the active mode (House Rule #6).
- The Indian/Dhan buckets face the same netting issue within Dhan; that is a
  separate, later application of this decision in Phase 3+.

## 020 — Regime retrain runs on the VM, not the Railway scheduler
Date: 2026-06-23
Status: Accepted
Refines: 014

Decision: The per-bucket HMM regime retrain runs on the **GCP Mumbai VM**
via a `systemd` timer (`ops/regime-retrain.timer` → `.service`, calling
`retrain_job --due`), **not** as an APScheduler job in the Railway
`scheduler` service. The Railway-side retrain registration is removed.

Rationale: The retrain trains on **Binance Futures klines**
(`fapi.binance.com`), and Binance geo-blocks Railway's region — every
Railway-side retrain returned `fetch_failed` / `n_bars=0` for all symbols.
Confirmed 2026-06-23: the identical job run from the VM trained all 5
symbols (Binance is reachable there, same as the bot's symbol-mapping
fetch). Models had silently gone stale since the 2026-06-14 manual seed.

Scope / mechanics:
- One **daily** timer (02:00 UTC); the job's `--due` mode enforces each
  bucket's `regime.yaml` `retrain_cadence` (weekly → Mondays, daily →
  every day, manual → never), preserving the old per-bucket semantics with
  a single timer. `--all` forces every enabled bucket; `--bucket <id>` runs
  one.
- The job posts the Telegram retrain summary itself (previously the
  scheduler wrapper did). `nightly_export` stays on Railway.
- Unit files are **not** auto-applied by the deploy pull-timer; they need a
  one-off `daemon-reload` + `enable --now` on the VM (see runbook).

Consequences:
- Inference was never affected (it reads bars from Delta, not Binance), so
  this only un-staled the regime models, not trading behaviour.
- A future non-crypto (Dhan) bucket would not hit the Binance block, but
  keeping all retrains on the VM timer is simplest; revisit if a bucket
  ever needs a data source only reachable from Railway.

## 021 — Exit engine, breaker enforcement, wallet-mirrored capital, dashboard auth
Date: 2026-07-06
Status: Accepted
Refines: 013, 015, 019
Related house rules: #3, #4

Context: A full-project review (2026-07-06 session) found four gaps that
made the pre-live soak unrepresentative: no exit path existed anywhere
(BucketRunner was entry-only; `select_exits` was never called), the
circuit breakers were defined but never invoked, `bucket_state` capital
was never updated after the migration seed, and the dashboard had no
authentication. User decisions recorded here:

1. **Exits are strategy-driven.** BucketRunner runs a step 0 before
   entries: every discovered strategy's `select_exits(held, data,
   regimes)` runs — including strategies currently blocked by the
   master/regime gate, since a gated strategy must still manage what it
   holds. Exit orders are reduce-only market orders; the Position row is
   flipped FLAT optimistically and the reconciler self-heals if the
   exchange disagrees.
   - `top5_volume` (longterm-crypto): hold until the symbol's regime
     flips to BEAR.
   - `ema_9_15` (swing-crypto): exit when EMA(9) sits below EMA(15)
     (state-based, so a missed cross bar still exits next tick).

2. **Direction is per strategy.** `EntryCandidate.side` is now honored by
   the runner (was hardcoded "buy"). Long-only vs long/short is a
   strategy property; the regime multiplier in `allocator.yaml` remains
   the per-bucket scaling knob.

3. **Breaker trip = halt + flatten.** `src/safety/enforcement.py` runs
   all breakers per sub-account every tick from `run_bot`. Any trip:
   engage the per-bucket kill switch (engaged_by="breaker"), then flatten
   every position on the account with reduce-only market orders.
   Reduce-only orders are allowed through an engaged kill switch
   (`allow_when_killed`); risk-increasing orders never are. Recovery is
   manual via the dashboard.

4. **bucket_state mirrors the sub-account wallet.** The reconciler syncs
   `available_balance_inr` (wallet available × allocator `fx_inr_per_usd`)
   and `locked_margin_inr` (order+position margin × fx) every sweep.
   Decision 019's one-account-per-bucket makes this 1:1; sharing an
   account across buckets would double-count and logs a warning.

5. **Dashboard uses HTTP basic auth.** All routes 401 without credentials
   when `DASHBOARD_PASSWORD` is set (constant-time compare). Unset ⇒
   serves openly with a startup warning — acceptable only while the URL
   is not shared. Tracebacks are no longer returned to the browser.

Also fixed under this decision (same session): transport-error recovery in
OrderManager (query the exchange by client_order_id before marking a trade
REJECTED, so a response timeout cannot double-fire next tick),
`set_leverage` failure now aborts placement instead of warn-and-continue,
and regime inference/training drop the in-progress candle (labels are
computed from closed bars only).

## 022 — Broker-side protective stop-loss on every position
Date: 2026-07-07
Status: Accepted
Refines: 021
Related house rules: #2, #8

Context: All exits so far are bot-driven (strategy exits, breaker
flatten). If the bot or its VM is down, nothing bounds the loss on an
open leveraged position short of the exchange's liquidation engine. The
2026-07-06 review flagged this as the biggest remaining risk gap before
go-live.

Decision: every open position gets an **exchange-resident reduce-only
stop-market order** so a max loss holds even with the bot offline.

Mechanics:
- **Config**: `stop_loss_pct` per bucket in `buckets.yaml` (percent of
  entry price). Seeded at 50% of bucket margin at `leverage_max`
  (0.5/leverage): longterm 10, swing 5, scalp 2, gambling 0.5 — well
  inside liquidation distance (~1/leverage). Indian buckets unset until
  Phase 3.
- **Sweep, not per-order hook**: `src/safety/stop_protection.py` runs
  once per tick per sub-account (after the bucket runners, so fresh
  entries are protected within seconds) plus once at startup. It diffs
  exchange positions against resting protective stops: missing → place;
  size mismatch (adds/partial closes) or trigger drift > 0.5% → cancel +
  re-place; stop without a position → cancel. Idempotent and
  self-healing; also protects positions that pre-date the feature or
  were opened manually.
- **Trigger**: entry_price ± stop_loss_pct, snapped to the product's
  live `tick_size`, fired on **mark price** (last-traded can wick on
  thin books). Delta body: `stop_order_type=stop_loss_order` +
  `stop_price` + `stop_trigger_method=mark_price`; untriggered stops
  rest in state `pending`, so `get_open_orders` now queries
  `states=open,pending`.
- **Through OrderManager**: stops get a Trade row (idempotent
  client_order_id, audit log, Telegram "STOP" alert, extra:
  `protective_stop`). If a stop fires while the bot is down, the next
  reconcile marks it FILLED and P&L enrichment pairs it with its entry
  like any other exit. Resting stops are excluded from the exit engine's
  in-flight-exit dedup so they never suppress a strategy close.
- **Kill-switch interaction**: placement uses `allow_when_killed` —
  protective stops are risk-reducing and must be maintained even while a
  bucket is halted (same rationale as the Decision 021 flatten path).

Consequences:
- A stop that fires shows up to the bot as a FILLED reduce-only trade +
  position gone; the sizer's 23h trade-dedup then blocks immediate
  re-entry on that symbol — desirable after a -10% move.
- The strategy exit path is unchanged; orphaned stops left behind by a
  strategy close are cancelled by the next sweep (≤1 tick). A triggered
  stop on an already-flat position is rejected by the venue (reduce-only)
  — harmless.
- `stop_loss_pct` is a safety parameter (like breaker thresholds), not a
  strategy parameter — no `backtest_ref` required to change it, but any
  change should still be committed with rationale.

## 023 — Daily-anchored drawdown breaker
Date: 2026-07-07
Status: Accepted
Refines: 021
Related house rules: #3, #8

Context: the `daily_drawdown` breaker was daily in name only — it
compared *current unrealized* PnL against *current* equity every tick.
Realized losses never counted (close a losing position and the breaker
resets), and the denominator shrank with the losses. A day of repeated
small realized losses could never trip it.

Decision: the breaker measures **total equity vs a fixed start-of-day
anchor**.

Mechanics:
- New table `daily_equity_anchor` (migration 0007): one row per
  (account_ref, UTC date), unique-constrained. Equity is in the
  account's settlement currency (USD for Delta India).
- The first breaker pass of each UTC day inserts the anchor with that
  moment's equity; later passes — including after restarts — read it
  back, so the anchor survives crashes (House Rule #3).
- Equity = wallet (available + order_margin + position_margin) +
  unrealized PnL, computed in `safety/enforcement.py` from the balances
  + positions it already fetches. `check_daily_drawdown` is now pure
  math: drawdown% = (anchor − current)/anchor × 100, trip at
  `DAILY_DRAWDOWN_PCT` (default 5%). Anchor helper:
  `src/safety/equity_anchor.py::get_or_create_daily_anchor`.
- `ops/deploy.sh` now runs `alembic upgrade head` (before the service
  restart) whenever a push touches `migrations/`; failure aborts the
  restart so old code keeps running against the old schema. Migration
  0007 itself was applied manually this session (the deploy cycle that
  ships this feature still runs the old script).

Known limitations (accepted for Phase 1):
- Mid-day deposits/withdrawals distort the measure (a withdrawal reads
  as a loss). Sub-account funding changes are rare and user-initiated;
  re-anchor manually by deleting the day's row if needed.
- While an account is fully halted, enforcement early-returns, so on a
  killed account the day's anchor is only created after un-killing —
  the first post-recovery pass anchors at recovered equity, which is
  the conservative choice.

## 024 — Kill-switch semantics: exits + breaker watch stay active while killed
Date: 2026-07-07
Status: Accepted (user chose option c, 2026-07-07 session)
Refines: 021, 023

Context: an engaged kill switch previously skipped the bucket entirely,
so a *manually* killed bucket ran no strategy exits, and an account
whose buckets were all killed was skipped by breaker enforcement. A
halted bucket holding positions was therefore unmanaged: no exit logic,
no drawdown/liquidation watch (only the exchange-resident stops from
Decision 022 remained).

Decision (user-selected option c): **the kill switch blocks
risk-INCREASING actions only.**

1. **Strategy exits run while killed.** BucketRunner's step 0 executes
   every tick regardless of kill state; exit orders pass the engaged
   switch via ``allow_when_killed`` (they are reduce-only). Scanner,
   regime, sizing, and entries remain fully blocked while killed.
2. **Breakers are watched while killed.** ``enforce_breakers`` no longer
   early-returns on all-killed accounts. A trip on a killed account with
   open positions still flattens them. Acting is gated: an
   already-halted, already-flat account with a persistent condition is
   watched silently (no re-engage / re-flatten / re-alert every tick).
   ``engage()`` is only called for switches not already on, so a manual
   kill keeps its original reason/engaged_by.
3. **Alert hygiene.** Per-breaker detail alerts and the enforcement trip
   alert are dedup-capped (3/hour per key) since breakers now evaluate
   every tick under persistent conditions; a one-off "breakers CLEAR"
   ping fires on recovery. The kill switch itself still only clears
   manually from the dashboard.

Also recorded under this session (supersedes part of the Phase 1c "live
FX" item): **USD/INR is a fixed rate, not a live feed** — user decision:
1 USD = 85 INR for Delta India. ``fx_inr_per_usd: 85.0`` in each
bucket's ``allocator.yaml`` is the single source; the frankfurter.app
fetch (``src/data_sources/fx.py``) was removed same-day. Live contract
sizes from ``/v2/products`` are unaffected and still used.

## 025 — Kelly sizes on live equity; capital_inr is the P&L baseline only
Date: 2026-07-07
Status: Accepted (user decision, 2026-07-07 session)
Amends: 015
Refines: 021

Context: the sizer computed Kelly notionals against the static
``buckets.yaml`` ``capital_inr`` (₹50k) while the actual sub-account
wallet held far less (testnet losses from the June duplicate-order bug
and liquidations left ~₹18k). Result: oversized suggestions that mostly
skipped as insufficient-margin, and a dashboard P&L of −63.9% measured
against capital the wallet no longer (and possibly never) held.

Decision (user): **sizing follows the account, P&L follows a baseline.**

1. **Kelly base = live equity.** ``size_positions`` uses
   ``bucket_state.available_balance_inr + locked_margin_inr`` (the
   exchange-mirrored sub-account wallet, Decision 021) as the Kelly
   capital base. The book automatically scales down after losses and up
   after gains/deposits. ``buckets.yaml`` ``capital_inr`` no longer
   affects sizing.
2. **P&L baseline = capital_inr + capital_adjustments_inr.**
   ``pnl = equity − (capital_inr + adjustments)`` with adjustments in
   ``bucket_state.extra["capital_adjustments_inr"]``. On any manual
   deposit (+X) or withdrawal (−X), record it with
   ``python -m scripts.record_capital_adjustment <bucket> --amount ±X``
   so the money movement doesn't read as P&L; ``--rebase`` zeroes the
   P&L at the current wallet. Every run writes an audit row.
3. **Executed this session:** longterm-crypto adjustments set to
   −31,736.34 (= 214.866592 USD × 85 − 50,000), writing off the
   June-era testnet losses; cumulative P&L now counts from the current
   ~$215 wallet.

Consequences:
- Sizing needs no config edits when funding changes; the adjustment
  script is only about keeping the P&L display honest.
- With equity as the Kelly base, the insufficient-margin skip
  effectively stops firing for fresh entries (weights ≤ aggregate cap
  1.0 of equity); it still guards when margin is already locked.
- A stale wallet mirror now affects sizing, not just display — the
  reconciler sync being healthy matters more (see the 2026-07-07 note
  about the suspected failing sweep on the VM).

## 026 — Multiple scanner sets per bucket (CSV-linked)
Date: 2026-07-08
Status: Accepted (user chose Option A, 2026-07-08 session)
Refines: 016

Context: a bucket had exactly one scanner.yaml + allocator.yaml, so every
strategy in it shared one universe and one allocation config. The user
wants strategies within a bucket to run against different scanning AND
allocation logic (e.g. a top-volume universe next to a momentum
universe, each Kelly-tuned separately).

Decision (Option A — CSV column, not scanner-owned subfolders):

1. **Named scanner sets.** A set ``<name>`` is a
   ``scanner_<name>.yaml`` + ``allocator_<name>.yaml`` pair in the
   bucket folder. The existing ``scanner.yaml`` + ``allocator.yaml``
   remain the default set (blank name).
2. **Linking via strategy_master.csv.** New optional ``scanner`` column
   (lowercase letters/digits/underscore); blank/absent ⇒ default set.
   The master CSV stays the single control sheet per bucket
   (Decision 016 intact); re-pointing a strategy is a one-cell edit.
3. **Runtime.** BucketRunner loads every named pair at boot (fail-fast
   when a referenced pair is missing) and runs ONE scan per set per
   pipeline pass, lazily. Sizing uses the strategy's set's allocator
   config (μ/σ, Kelly fraction, caps, regime multipliers, fx). All sets
   share the bucket's one capital pool (live equity, Decision 025) and
   sub-account; the per-set aggregate_cap bounds each book and the
   insufficient-margin skip arbitrates between them.
4. **Persistence.** Named scans write DailyUniverse / ScannerSnapshot
   rows under ``strategy_id = "<bucket_id>:<name>"`` so they don't
   collide with the default scan's (date, strategy_id, symbol) unique
   key. Regime (per bucket), exits, breakers, and stops are unaffected.

## 027 — Indian buckets size on a capped allocation, not the raw wallet
Date: 2026-07-12
Status: Accepted
Amends: 025 (for Market.INDIAN only) · Context: 013, 019

Context: crypto buckets are isolated by FUNDING — each has its own Delta
sub-account (Decision 019), so "size on live wallet equity" (Decision 025)
naturally equals "size on the bucket". **Dhan has no sub-accounts.** The
Indian buckets share one brokerage account, and the Dhan sandbox wallet is
a fixed ₹10,00,000 that resets daily. Unmodified Decision 025 would have
swing-indian Kelly-sizing on ₹10L (sandbox) or on whatever the shared live
account holds — 20× the intended ₹50k bucket, and no isolation from a
future longterm-indian bucket in the same account.

Decision: ``sizing_equity()`` in ``src/shared/allocator/sizer.py``:

- **Crypto** — unchanged: live sub-account wallet (available + locked
  mirror); profits compound into sizing automatically.
- **Indian** — ``min(wallet_equity, capital_inr + capital_adjustments)``.
  The bucket sizes on its allocation, never on shared/simulated money it
  doesn't own; a wallet below the allocation still floors at the wallet.
  Compounding is deliberate: record positive adjustments via
  ``scripts/record_capital_adjustment.py`` (or raise ``capital_inr``,
  YAML-audited).

Consequences:
- Sandbox soak numbers read exactly like the real ₹50k bucket.
- When longterm-indian joins the same Dhan account, each Indian bucket is
  bounded by its own allocation — capital isolation without sub-accounts.
- P&L baseline semantics (Decision 025) are untouched; the same
  adjustments field feeds both.

## 028 — swing-indian: wide catastrophe stop (amends 022's 0.5/leverage rule)
Date: 2026-07-12
Status: Accepted (user chose "wide catastrophe stop", 2026-07-12 session)
Amends: 022 (for swing-indian only)

Context: Decision 022 mandates an exchange-resident protective stop per
position at 0.5/leverage distance (≈ 50% of margin). The Blasting Momentum
backtest showed tight stops DESTROY this edge — day-1 noise knocks out
eventual winners; exits belong to the strategy (Supertrend(10,3) flip or
the 30-day cap).

Decision: swing-indian keeps a broker-side stop, but WIDE:
``stop_loss_pct: 20`` in buckets.yaml (~60% of margin at 3× MTF). It is a
crash net for when the bot/VM is down, deliberately far outside normal
strategy exits so it never interferes with the backtested behaviour.
All other buckets keep the 0.5/leverage rule.

## 029 — Seventh bucket: intraday-indian (amends 013's fixed six)
Date: 2026-07-21
Status: Accepted (user chose "new intraday-indian bucket", 2026-07-21 session)
Amends: 013 · Context: 012, 022, 026, 027, 028

Context: the Backtesting Engine produced a holdout-validated NIFTY-100
gap-down reversal strategy and a handoff spec written for this repo
(`Backtesting Engine/strategies/optimized/nifty100_gap_reversal/
TRADING_BOT_HANDOFF.md`, 2026-07-20). It is **intraday** — entries
09:30–10:30, square-off 15:15, no overnight risk — and Decision 013's six
(type × market) buckets have no such slot. Two paths were on the table:
a new bucket, or a second scanner set under swing-indian (Decision 026).

Decision: **new bucket `intraday-indian`** (path A). `TradingType` gains
`INTRADAY`. The strategy is genuinely a different animal from swing —
different cadence (5m vs 1d), different holding period (hours vs weeks),
different product (MIS vs MTF) — and forcing it under swing-indian would
have it fighting that bucket's 09:45 daily-prepare machinery for no saving,
since an intraday scan path was needed either way.

Validation to plan around (holdout fold, frozen, contains the Apr-2025 crash):
35 trades, +13.3% on deployed margin, win 51%, PF 1.68, maxDD 9.2%. NOT the
full-period +36.6%. Flow is thin — ~0.8 trades/week; zero-signal days are
normal and must not be "fixed" by loosening filters.

Sub-decisions:

- **Capital ₹1,00,000, not the house ₹50,000.** This is a cost decision, not
  a conviction one. The backtest was validated at ₹1L notional per trade; at
  the frozen 20% per-symbol cap and 5× MIS that requires ₹1L of bucket
  capital. The engine measured costs eating ~99% of gross at ₹10k/trade, so
  under-capitalising this bucket moves it below its cost hurdle rather than
  merely scaling it down. Total allocation across buckets goes ₹3L → ₹4L.
- **Regime gate OFF** (`regime.yaml enabled: false`, blank regime columns in
  strategy_master.csv, and all three `regime_multipliers` pinned to 1.0).
  This strategy fades panic: its signals are *generated by* bearish tape, and
  the frozen holdout that earned the +13.3% contains the April-2025 crash. A
  bull/neutral gate would mute it exactly when it works. Deliberate departure
  from Decision 014's per-bucket HMM; revisit only with a backtest showing a
  gate helps.
- **Wide catastrophe stop, `stop_loss_pct: 15`** — same reasoning as Decision
  028 for swing-indian. Every tight stop tested LOST (pattern-low RR 1–3, 5m
  trailing): the reversal needs hours and noise-stops fire first. The stop is
  a crash net for a bot/VM outage, far outside the 15:15 square-off's range.
- **Per-bucket entry window.** `nse_session` was hardcoded to swing-indian's
  09:45–10:30. It now takes the window as arguments, sourced from new
  `entry_start`/`entry_end` fields in buckets.yaml (defaults reproduce the old
  constants, so no existing bucket changes behaviour). intraday-indian uses
  09:30–10:30.
- **Corporate-action guard is reformulated for live.** The engine compares
  today's daily-series gap against the intraday one — scale-invariant, but it
  needs TODAY's daily bar, which Dhan does not publish until well after the
  close (the 2026-07-14 STALE-CLOSE bug), so it is unusable at 09:30. Live we
  compare the intraday gap against the same gap recomputed on the adjusted
  daily prev-close. Consequence, measured and accepted: replaying a date that
  precedes a corporate action sees the two series on different scales and
  skips the name (1 of the 76 frozen trades — VEDL 2025-08-26, daily rescaled
  ×0.374 by the later Vedanta demerger). The scale-invariant alternative
  (consecutive within-series close ratios) was tried and is worse: the 5m
  series' last bar closes 15:25 and misses the closing auction, disagreeing
  with the official daily close by ~1% on ordinary days, which rejected IOC
  and TORNTPHARM on noise. Skipping is the safe direction.

Consequences:
- A seventh bucket exists; "six buckets" in older docs now reads as six + one.
- Parity with the frozen backtest is verified, not assumed: replaying all 76
  trades through the ported screen + pattern math reproduces 75 (the VEDL
  exception above), including exact pattern name and entry bar time.
- The bucket ships **disabled** (`enabled: false` in buckets.yaml, seeded
  `false` in migration 0009) and is switched on by hand after review.

## 030 — intraday-indian: broad universe, MIS routing, shared capital, ledger P&L
Date: 2026-07-21
Status: Accepted (user decisions, 2026-07-21 session)
Amends: 029 · Context: 012, 019, 021, 026, 027

Five follow-ups to Decision 029, all user-chosen.

**1. Capital back to the house ₹50,000.** ₹50k × 5x is a ₹2.5L notional
ceiling, so 5 slots of ₹1L (the backtested size) is arithmetically
impossible; `per_symbol_cap` stays 0.20, giving 5 slots × ₹50k notional.
The cost penalty is small — the ₹20 brokerage cap is 0.03%/leg at ₹50k vs
0.02% at ₹1L, so round-trip goes 0.10% → 0.12% against a ~0.62% mean trade
(~3% of the edge). The real cliff is ₹10k, where brokerage alone is
0.2%/leg; ₹50k stays well clear.

**2. Broad scanner set (Midcap 150 + Smallcap 100), NOT validated.**
Decision 026 named sets, so `scanner.yaml` stays the frozen NIFTY-100
config and `scanner_broad.yaml` carries 235 names (the two indices minus 15
NIFTY-100 overlaps, which would otherwise double-enter — dedup is per
bucket+strategy+symbol and the sets run as different strategy names). The
holdout grade PF 1.68 does NOT extend to it; the gap-reversal learnings
call a midcap universe "a separate study; re-freeze a holdout", which has
not been done. It runs to generate that evidence with separately
attributable P&L. `gap_down_reversal_broad` subclasses the validated
strategy so the entry/exit logic can never silently diverge.

**3. Circuit filter — and a corrected premise.** The obvious filter ("hard
band ≥ 20%") is actively wrong: measured 2026-07-21, only **2 of 99**
NIFTY-100 names have a 20% band, because 97 are F&O underlyings whose
SM_UPPER/LOWER band is *dynamic* (it widens after a cool-off rather than
freezing). A naive width filter would have rejected the validated universe
outright. The implemented test is **F&O underlying OR hard band ≥ 20%**,
which excludes only 5 of the 235 broad names. Non-F&O narrow-band scrips
are the real hazard: a continued slide can lock them, stranding a long
until auto square-off — a tail the NIFTY-100 backtest never faced. Scrip
metadata (`fno`, `band_pct`) now rides in the Dhan universe cache.

Residual risk accepted: 82 of the 100 smallcaps are non-F&O, so even at a
20% hard band they CAN lock intraday. The validation does not cover this.

**4. MIS routing, sized to granted margin.** Dhan had no MIS path at all —
the client was MTF-only with a CNC fallback, so this bucket would have
routed MTF (funded delivery, ~14–18% p.a., no auto square-off). `product`
is now per-ORDER (Dhan has one account, so both Indian buckets share one
adapter): swing-indian sends MTF, intraday-indian sends INTRADAY. **No CNC
fallback for INTRADAY** — falling back would convert a 5x same-day trade
into a 1x overnight delivery position, so an MIS-ineligible scrip fails
loudly instead.

Leverage is never predicted, and `leverage_max: 5` is a risk CEILING, not a
target. NSE cash leverage is graded per scrip: measured 2026-07-21 the
median is **4.44x** across NIFTY-100, **3.79x** across Midcap 150 and
**3.06x** across Smallcap 100 — and **not one name in any of the three
reaches 5x**. Sizing every position at a flat 5x would over-order on
effectively every trade and collect an RMS rejection.

Two sources, in order of authority:
1. `Broker.required_margin` — the venue prices the exact order (Dhan
   `/v2/margincalculator`). Scaling to it deploys the full margin budget at
   whatever multiple is truly allowed, which IS "trade at max leverage".
2. `MarketData.max_leverage` — the scrip master's per-scrip figure, capped
   at the bucket ceiling, used when the venue offers no preflight.
Neither available ⇒ 1x, the only size guaranteed affordable.

CAVEATS. (a) `/v2/margincalculator` is unexercised against a live account as
of 2026-07-21; the first sandbox soak is its acceptance test. (b) The scrip
master has NO intraday leverage column (`BUY_CO_MIN_MARGIN_PER` and
`BUY_BO_MIN_MARGIN_PER` are all zero, `COVER_FLAG` all null), so source 2 is
the **MTF** (funded-delivery) figure. Both are graded off the same exchange
risk parameters and intraday risk is lower than overnight, so it should
under-state what MIS allows — safe as a fallback, not a substitute.

EXPECTATION SET BY THIS. The backtest's "+13.3% on margin" assumed 5x. P&L
scales with notional, so at a 4.44x median the same margin buys ~89% of the
backtested exposure, and ~61% at the smallcap median of 3.06x. Return ON
MARGIN should be expected to land proportionally below the backtest — this
is a real haircut, not a bug, and it is the price of not over-ordering.

**5. Shared capital budget, and per-bucket P&L.** Two holes surfaced from
one root cause — components reading a `bucket_state` mirror that only
refreshes on the reconciler sweep:

- *Sizing*: `size_positions` ran once per strategy, each sizing against the
  full bucket capital, so two sets at `aggregate_cap: 1.00` could each claim
  100% (200% of the bucket) in one tick. The runner now threads a running
  `committed_margin_inr` through, and the budget is capped at
  `min(wallet, capital)` — the raw Dhan wallet is not the bucket's money
  (Decision 027). Slots are first-come-first-served; strategies iterate in
  sorted filename order, so the validated set claims ahead of the broad one.
- *Dashboard*: cumulative P&L came from mirrored wallet equity, which for
  Indian buckets is the SAME shared Dhan balance (the reconciler already
  warned "capital double-counted"). Latent while swing-indian was the only
  Indian bucket. Indian buckets now use `bucket_ledger_pnl` — realized +
  unrealized from that bucket's own Trade rows. Crypto keeps the wallet
  mirror, which is correct there (Decision 019 sub-accounts).
