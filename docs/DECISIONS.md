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

---

## 031 — intraday-indian: MIS-ineligible scrips fall back to 1x CNC
Date: 2026-07-27
Status: Accepted (user decision, 2026-07-27)
Amends: 029, 030(4)

Decision 030(4) ruled **no CNC fallback for INTRADAY**: an MIS-ineligible
scrip failed loudly, because retrying as CNC would convert a leveraged
same-day trade into a 1x overnight *delivery* position. Presented with the
trade-off, the user chose to **trade it at 1x CNC rather than skip it**.

**The hazard this had to solve.** A naive product swap is a silent
over-spend. `size` reaching the broker was sized for leveraged MIS — at 4x a
₹10k slot buys ₹40k of stock — and CNC is a *cash* product, so re-sending
that same quantity demands the full ₹40k, four times the margin the sizer
budgeted for the slot. Repeated across 5 slots it would try to spend ₹2L of a
₹50k bucket.

**Implementation — the fallback is always sized 1x.** `OrderRequest` carries
`fallback_max_size`, the quantity affordable with NO leverage
(`margin_budget / price`, floored), computed by
`BucketRunner._one_x_size`. On an INTRADAY rejection the Dhan adapter retries
as CNC with `min(size, fallback_max_size)`. Guards:

- **Opt-in per bucket.** `fallback_product: CNC` in `buckets.yaml`; a bucket
  that omits it passes no cap, and the adapter lets the rejection propagate
  (`swing-indian` keeps its separate MTF→CNC path unchanged).
- **Never upsizes.** The cap is a `min`, so it can only reduce.
- **Never places a stub.** A cap below 1 share re-raises rather than sending
  a 0-quantity order.
- **The Trade row records what was actually placed.** `OrderResult.size`
  reports the clamped quantity and `OrderManager` writes it back to
  `Trade.quantity` (it previously only updated status/exchange id). Ownership
  scoping (`net_owned`), P&L and stop sizing all read that column, so a
  stale leveraged quantity there would have mis-scoped every one of them.

**Residual risk the user accepted.** (a) CNC has no broker auto-square-off
net, so if the 15:15 square-off fails the position is held overnight as
delivery — the exact exposure 030(4) avoided. (b) At 1x the notional is
₹10k, where the ₹20 brokerage cap is 0.2%/leg ≈ 0.4% round-trip against a
~0.62% mean trade — Decision 030(1)'s "real cliff". The fallback therefore
trades a thin-to-negative edge in exchange for participating at all; it is
expected to be rare, since MIS-ineligibility is uncommon among liquid names.

---

## 032 — swing-indian goes live on Midcap-150 1h mean reversion
Date: 2026-07-27
Status: Accepted (user decision, 2026-07-27)
Amends: 028 (the bucket's stop), 031 (the MTF leg of the CNC fallback)
backtest_ref: `Backtesting Engine/strategies/optimized/midcap150_meanrev_1h_swing/`
(TRADING_BOT_HANDOFF.md + mean_reversion_1h_swing_rules.json; engine
`mean_reversion_1h_scanner.py`; findings in
`results/learnings/2026-07-23_meanrev_1h_nifty100.md`)

The backtester handed over a holdout-validated 1h mean-reversion swing
strategy. The user chose to take it **live** (real money, quarter scale) rather
than stage it on sandbox. It replaces Blasting Momentum as the bucket's
strategy; that one never traded live and is now inert.

**The strategy.** Evaluated on every completed 1h bar (resampled from Dhan 15m,
anchored per IST day to 09:15 — a TradingView 1h chart) across NIFTY-Midcap-150
∩ NSE F&O (94 names). `dist = (close/ema20 − 1) × 100`; a **fresh** downward
cross of −6.5% (`dist[t] ≤ −6.5 AND dist[t−1] > −6.5`, so a stuck-stretched name
does not re-fire) goes LONG at the next bar's open. Exits: 1h close ≥ EMA20
(mean touch, the primary), a 3.5 × daily-ATR14 protective stop, or 20 trading
days. Long-only, ≤5 new entries/day, one position per symbol.

Validated 24 months at ₹10k margin/trade on per-stock MTF (avg 3.79×):
train (2025-07→2026-07) 82 trades +24.7% PF 2.31 DD 4.4%; frozen holdout
(2024-06→2025-06) 132 trades +57.1% PF 3.04 DD 5.3%. **Plan around the TRAIN
grade** — the holdout is crash-boosted, because this is a convex buy-the-panic
book. Monte Carlo: day-cluster bootstrap P(net≤0) = 0.00%.

### The four decisions this needed

**(1) Quarter scale, not full.** The backtest's unit of risk is a FIXED ₹10,000
of own capital per trade, with `capital_base = 20 slots × ₹10k = ₹200k`. The
bucket keeps `capital_inr: 50000`, and `per_symbol_cap: 0.20` turns that into
exactly ₹10k/slot — so per-trade economics (≈₹38k notional at MTF) match the
validated run and only the book size differs: 5 concurrent names, not 20. A 6th
simultaneous signal is skipped as `SKIPPED_INSUFFICIENT`, never sized down. To
scale up, raise `capital_inr`; never `per_symbol_cap`.

**(2) The old strategy goes inert, not live alongside.** Enabling the bucket
would have armed Blasting Momentum's order path, which has never run outside
the Dhan sandbox. It is now `_blasting_momentum.py` (leading underscore ⇒ the
loader skips it) with no `strategy_master.csv` row, and its configs live at
`scanner_blasting.yaml` / `allocator_blasting.yaml`. Consequences worth
knowing: the bucket's DEFAULT scanner set is now `equity_meanrev_1h`, so the
nightly `dhan-prepare` timer (which only visits `equity_daily` buckets) no
longer sweeps 4,600 names for it; and `regime.yaml` is now **disabled**,
because the bull/neutral gate existed for Blasting Momentum and is actively
harmful here — in-sample it cut net +30.7% → +2–9% by removing the
crash-rebound trades that ARE the edge.

**(3) The ATR stop RESTS on the exchange.** Decision 028 gave this bucket a
wide 20% catastrophe net because Blasting Momentum's edge died with tight
stops. This strategy's backtest is validated WITH a stop, at
`entry − 3.5 × daily_ATR14` (≈9–12% on a midcap), modelled as gap-through — so
a tick-checked exit would be both less faithful and useless while the bot is
down. `plan_stop_protection` therefore takes an optional per-symbol
`stop_distances` map: the strategy emits the rupee distance in its
`EntryCandidate.hint`, `BucketRunner` stamps it on the entry `Trade.extra` (via
a new `OrderManager.place_order(extra_payload=...)`), and the sweep reads it
back. **The override can only ever tighten** — a distance wider than the
bucket's own percent net is refused and logged, so the configured worst case
holds no matter what the ATR read does. Buckets that supply no distance behave
exactly as before.

**(4) The bot books MTF interest itself.** The backtest omits Dhan's funding
cost (measured ~₹6,456 on ₹163,636 of net, ~4%). `carry_interest_apr: 0.146` on
the bucket makes the reconciler subtract
`(notional − margin) × rate × days/365` from realized P&L when a round-trip
closes — funded portion only, so the 1× CNC fallback pays nothing. Reporting a
P&L the strategy never earned would corrupt every downstream read.

### Cadence — the one structural change

Entries fire intraday on ANY 1h close, and mean-touch exits need 1h checking,
neither of which the bucket's 09:45 daily-prepare shape supported. Rather than
a new bucket or a new runner mode, three small changes covered it:

- the entry window is now the whole session (`09:15`–`15:30`), with the REAL
  gate bar-driven inside the strategy — a signal is actionable only while its
  bin is the most recently completed one, which is the live equivalent of the
  backtest's "fill at the next bar's open" (House Rule 9: the same rule replays
  in the backtester);
- `BucketRunner` now paces a bucket at its FASTEST timeframe rather than the
  regime model's, with an explicit `tick_interval_seconds: 60` override — a 1d
  regime model would otherwise have delayed every 1h signal by up to 15 min;
- `run_meanrev_scan` caches per 1h bin, so a 60s tick loop re-fetches 94
  symbols × 2 series only when a new bar closes (~190 calls per bin, not per
  minute).

Note the 15:15→15:30 stub bin: a 1h session has 7 bins, and the last is 15
minutes long. It is a real signal bar in the backtest (3 of 214 trades entered
at the FOLLOWING session's 09:15 open), so `last_complete_bar_key` keeps naming
it until the next session's first bin closes.

### Hazard found while wiring this up

Taking MTF live exposed a pre-existing gap in the Dhan adapter: the MTF→CNC
fallback re-sent the **leveraged** quantity as a cash order. At ~3.8× that is
~₹38k of cash for a slot budgeted ₹10k — on an account shared with the user's
own money (Decision 027). Decision 031 had already solved exactly this for
INTRADAY; the two branches are now one, so **any** leveraged-product rejection
retries at `min(size, fallback_max_size)` or not at all. `swing-indian` opts in
with `fallback_product: CNC`.

### Parity

`scripts/meanrev_1h_parity.py` replays the port over the same cached 15m/1D
CSVs the backtest used: **208/214** trades reproduce on all three axes (fresh
cross on the same bin, dist within 0.15pp, ATR stop within 0.1%). The six
misses are all the EMA20 warm-up guard at a data boundary — five on 2024-06-04
(the cached series starts 2024-05-15, giving 96 of the 100 required 1h bins)
and WAAREEENER, an IPO with 11 daily bars and therefore no ATR at all. They are
₹13,660 of net, all winners, so the port is conservative rather than broken;
live, the guard only bites a recent listing, which is where a seeded EMA and a
missing stop argue for sitting out anyway.

### Known deviations from the backtest, accepted

- **5 concurrent instead of 20** (see (1)) — expect ~a quarter of the rupee
  P&L, and occasional missed signals when the book is full.
- **Fills are market orders moments after the bar close**, not the exact next
  open. Same convention as `gap_down_reversal`; a signal found late is skipped
  rather than chased.
- **Mean-touch exits fill at the next bar's open**, not at the touching close.
- **Cold-EMA / no-ATR names are skipped** (the 208/214 above).

---

## 033 — Session invariants: a process watchdog, and the agentic perimeter's authority ceiling

**Date:** 2026-07-28 · **Status:** Tier 1 landed; Tiers 2–3 designed, not built

### Why

Two buckets now trade real money (`intraday-indian` 2026-07-22,
`swing-indian` 2026-07-27) and the only automatic protection is
`breakers.py`, which asks exactly one question: *has equity fallen off a
cliff?* That is the right question for a leveraged crypto sub-account.
It is the wrong question for Indian equity, whose failure modes are
**procedural** and every one of which can happen at a perfectly healthy
equity:

- `intraday-indian`'s 15:15 square-off lives inside the STRATEGY's exit
  (`gap_down_reversal.exits`), driven by the latest bar's timestamp. A stale
  feed, a tick error, or a rejected exit each leave the position open — and
  on the CNC fallback (Decision 031) there is no broker auto-square-off net
  behind it. A leveraged intraday trade silently becomes an unwanted
  overnight delivery.
- A bot-owned position can end a tick with **no resting protective stop**
  (Decision 022) when placement failed. Equity is fine; the crash net is not
  there.
- A sizing bug over-commits capital long before it shows as drawdown.
- A single bucket's runner can wedge while the process heartbeat keeps
  beating for the others — `core/heartbeat.py` is process-level, so one dead
  bucket is invisible to the dead-man's switch.

### The authority ladder (the load-bearing part)

```
L3  FLATTEN positions        ← deterministic breakers ONLY. Never an LLM.
L2  HALT account             ← deterministic invariants
L1  HALT bucket entries      ← invariants, and (Tier 2) the supervisor agent
L0  Telegram notice          ← default
```

Enforcement is capped at **HALT**: engage that bucket's kill switch, which per
Decision 024 blocks risk-INCREASING actions only — strategy exits, the stop
sweep and the breakers all keep running while killed. Flattening stays in
`enforcement.py`, reachable only by a deterministic breaker trip.

This is what keeps House Rule #1 intact when Tier 2 lands. An invariant — and
later an agent — is an assertion about **process**, not a view on the market.
Engaging a kill switch is strictly risk-reducing and reversible; closing a
position is a trading decision and stays deterministic. Recovery is manual
from the dashboard, matching how a breaker trip already behaves.

### Tier 1 — `src/safety/session_invariants.py` (landed)

Runs once per 60s tick per account, **after** the stop sweep, so "no stop"
means the sweep failed rather than hasn't run yet.

| Invariant | Severity | Note |
|---|---|---|
| `squareoff` | HALT | intraday products only, 15:15 + grace |
| `stop_coverage` | HALT | needs 2 consecutive ticks (races a just-placed stop) |
| `notional_ceiling` | HALT | INR-native equity buckets only |
| `reject_rate` | HALT | ≥3 rejects / 15 min |
| `bucket_liveness` | NOTICE | per-bucket heartbeat row |
| `foreign_positions` | NOTICE | **never acted on** — that is the user's book |

**Ships OBSERVE-ONLY.** `session_invariants_enforcing` defaults to `false`:
every check runs and pages, prefixed `[OBSERVE-ONLY, would have HALTED]`, but
the kill switch is never touched. No invariant has fired against a real
session, and an untested check that can halt a live bucket on its first false
positive is a worse trade than a Telegram message. Sustain streaks keep
counting while observing, so flipping the flag needs no warm-up. Flip it once
the alerts have agreed with reality for a few sessions.

Two design points worth keeping:

- **`effective_holdings` intersects two independent sources.** The Trade
  ledger alone goes stale the moment something closes a position without
  writing a SELL row — Dhan's own MIS auto-square-off does exactly that, and
  would leave a phantom holding failing the square-off invariant forever.
  Exchange positions alone cannot tell the bot's rows from the user's on the
  shared account (Decision 027).
- **The notional ceiling is INR-equity only.** Delta positions are
  contract-denominated and USD-priced, so `qty × entry_price` is neither a
  base-unit size nor rupees. `BucketWatch.notional_budget_inr=None` skips it.

### Tier 2 — intraday supervisor agent (designed, not built)

An LLM agent at ~6 fixed points (09:15, 09:30, 10:30, 12:00, 15:10, 15:20 IST)
reading a Postgres-only snapshot. Catches what thresholds cannot: entry rate
far off the backtest baseline, every fill at its bar's extreme, a symbol
cycling in and out, a `sizing_snapshot` skip reason spiking. Authority: L1
halt, then page.
### Tier 3 — `src/reporting/eod.py` (landed 2026-07-28)

Scheduler job at 10:15 UTC = **15:45 IST**, after the 15:15 square-off and the
15:30 close, so what is still open genuinely IS carried overnight. Weekdays
only, and it re-checks `is_trading_day` (an NSE holiday has no session to
report on).

The nightly Parquet export already archives the ledger at 06:00 IST the
morning after. That is an archive, not a report: it says what was traded and
nothing about whether the session behaved. This answers the questions you
actually have at 15:45 — what each bucket made, **which signals did not trade
and why** (`sizing_snapshot` has held that record since Decision 026 and
nothing had ever read it back), what tripped, what is carried overnight, and
whether the live edge tracks the backtest.

Three outputs, one source:

- **Telegram digest** — phone-readable, no tables, truncates a long event list
- **`session_report` table** (migration 0011) — Postgres is the store because
  the Railway scheduler container is ephemeral and holds no git credentials.
  One row per date; a re-run UPSERTs rather than piling up near-duplicates.
- **`/journal` dashboard route** + `scripts/export_journal.py`, which
  materialises rows into `docs/journal/*.md` for git. Committing is opt-in
  (`--commit`) and safe on the VM: `ops/deploy.sh`'s `RESTART_PATHS` excludes
  `docs/`, so a journal commit never restarts the bot.

The dashboard renders the markdown with a ~60-line local subset renderer
rather than a CommonMark dependency — the generator emits a known, closed
grammar (headings, tables, lists, bold, code spans). It **escapes before it
formats**, so a broker message or symbol containing angle brackets cannot
inject markup.

A quiet day says so plainly instead of rendering an empty skeleton. Both live
strategies wait for a specific setup and most days do not offer one; a report
that looks broken on a normal day is a report you stop reading.

`scripts/eod_report.py` builds one by hand for a backfill or a re-run.

### Signal price at decision time (added 2026-07-28)

The prerequisite for both deferred sections, and the reason they were deferred:
**"what the strategy saw" is not something the exchange knows.** It is
unrecoverable unless recorded at the moment of the decision, so every day it
went unrecorded was a day of live evidence permanently lost — with two buckets
newly on real money, that was the most expensive thing still missing.

Three prices now ride on every entry `Trade.extra` (no migration — the
`hint` → `_entry_extra` → JSONB path already carried `stop_distance`):

| key | meaning | written by |
|---|---|---|
| `signal_price` | close of the bar the strategy decided on | the strategy's `hint` |
| `decision_price` | mark when the runner actually placed the order | `BucketRunner` |
| `avg_fill_price` | what the exchange gave us | the reconciler (already existed) |

Which splits the gap into two costs that have **completely different fixes**:

```
decision lag = decision_price − signal_price   → scan latency / tick cadence
execution    = fill_price − decision_price     → spread, impact, order type
```

Reporting only the total would say "you are losing 16bps" without saying which
one to go and fix. Sign convention: **positive is always a cost**, on both
sides of the book, so entries and exits can be averaged without cancelling into
a comforting zero.

Exits carry `decision_price` but no `signal_price` — `select_exits` returns
bare symbols, so there is no per-symbol reference bar to read without changing
that contract for every strategy. The execution half is the actionable half
anyway. Cost: one extra `get_ticker` per exit, exception-safe and ~0.22s under
Dhan's pacing, which is negligible against the 15:15→15:30 square-off window.

### Live vs backtest

`backtest_baseline` in each bucket's `allocator.yaml` — the file that already
carries `backtest_ref` and the pooled mu/sigma, so House Rule 7 holds. Purely
descriptive; **the sizer never reads it**. Every field is optional, and
`win_rate` is deliberately blank for both live buckets: it is not recorded
anywhere in this repo, and a guessed baseline is worse than none.

Profit factor and win rate are scale-invariant, which is what makes a live
figure in rupees directly comparable to a backtest figure computed on unlevered
returns. A live PF with no losing round-trip yet is reported as **undefined,
not infinite** — printing ∞ beside a backtest's 2.31 would read as spectacular
rather than as "too early to say".

Below `MIN_TRADES_FOR_SIGNAL` (20) closed round-trips the section leads with a
"too early to read" banner. Both buckets went live in July 2026, so every
report for months will be under it, and a 3-trade profit factor of 4.90 must
not read as a verdict.

**Hard constraint on both:** they read **Postgres only** and must never call
the Dhan API. A second session evicts the bot's token — a monitor that polled
the broker would cause the very outage it exists to detect.

**Placement:** not on the Mumbai VM. A dead VM must not be able to silence
its own watchdog — the same reasoning that put the heartbeat watch on Railway
(Decision 020's geo-block does not apply, since neither tier touches Binance).

---

## 034 — The stop rides on the entry order (Dhan Super Order)
Date: 2026-08-17
Status: Accepted, shipped behind a flag (OFF)
Amends: 022 (for Dhan equity only), 032
Related house rules: #2, #7, #8

Context: Decision 022 protects a position with a SEPARATE reduce-only
stop-market order, rested by a 60-second sweep AFTER the entry fills. That
ordering has a window in it, and in the week of 2026-08-11 the window produced
four production bugs:

| Failure | Cause |
|---|---|
| Attribution race | the sweep ran seconds after the fill, before any `Position` row existed — twice (`_load_attribution`, then `_load_stop_distances` ten lines below it) |
| Wrong product | the stop went out as MTF against an INTRADAY position; Dhan rejected it 116 times |
| Duplicate stops | the adapter hardcoded `reduce_only=False`, so the sweep could never recognise a stop it had already placed |
| **Naked position** | swing-indian's first ever fill (PIIND, 15 @ 2514.50) opened, and the stop was then found to be unplaceable — the position sat unprotected and the bucket halted itself |

The last row is the one that matters. The bot entered a trade and only
afterwards discovered it could not protect it. Three of the four fixes were
patches around a race that a different design does not have.

Decision: on Dhan, an entry is placed as a **Super Order** — entry, target and
stop-loss legs in ONE request (`POST /v2/super/orders`) — so the protective
stop is accepted or refused **with the entry, not after it**.

The property being bought is not "a faster stop". It is the inversion of the
failure mode: **an unplaceable stop now means the trade does not happen.**
Fail-safe instead of fail-open.

### What this does NOT change
- **Crypto is untouched.** `Broker.supports_attached_stop()` is False by
  default; Delta India keeps the Decision 022 sweep exactly as it was.
- **The sweep stays.** It still protects legacy positions opened the old way,
  still runs for crypto, and is still the net if this path is turned off.
- **The stop distance is unchanged** — Decision 032's 3.5 × daily ATR14, via
  the same arithmetic (now `resolve_stop_trigger`, shared by both paths so
  they cannot drift apart the way the two loader functions did).

### The mandatory target leg
Dhan requires `targetPrice`. Neither strategy has a target — swing-indian
exits on the band re-cross, intraday holds to 15:15 — so the leg is cancelled
(`DELETE .../TARGET_LEG`) the moment the entry is accepted.

The obvious implementation is wrong. A target placed "far enough away that it
can never fill" sits **outside the scrip's daily circuit band**, and Dhan
refuses an out-of-band price at validation — which here would reject the WHOLE
super order, entry included. That is the PIIND failure again with the entry as
the victim instead of the stop. So the target is placed **just inside the
band** (the furthest it can legally be), then cancelled. A failed cancel is
recorded on the Trade row, retried by the sweep, and alerted: a surviving
target is a live exit at a price no backtest justifies (House Rule 7).

### The naked-short guard (the crux)
A `STOP_LOSS_LEG` rests independently of our position. If a strategy exits and
the leg is not retired first, it later triggers against stock we no longer
hold — **on MTF that opens a short**, which is worse than the missing stop this
decision exists to fix.

Every closing path — `BucketRunner._close_position` and
`enforcement._flatten_positions` — funnels into `place_order(reduce_only=True)`
and reaches the Dhan adapter, so the retirement happens **in the adapter**: one
chokepoint, covering both paths and any written later, without teaching the
broker-agnostic layer about Dhan leg semantics.

Ordering is strict, and failure is fail-CLOSED: if the leg cannot be cancelled,
or the lookup that finds it fails, **the closing order is not sent**. Refusing
to sell is recoverable — the position remains protected by the very leg we
could not cancel. Selling twice is not.

### Ownership on the shared account (Decision 027)
It is **unverified whether Dhan echoes `correlationId` onto super-order legs**,
and it cannot be verified without placing a real order. If it does not, a
correlationId-only ownership check would classify every one of our own super
orders as the user's: the bot would never retire a stop before selling, and
`stop_coverage` would read every position as uncovered and HALT the bucket
every tick — the invariant firing *because* the stronger protection was used.

So ownership takes two independent proofs: the correlation id, and a ledger
lookup (`Trade.exchange_order_id`) injected into the adapter. The ledger proof
also survives a restart, which an in-memory set would not. Neither proof ⇒ not
ours ⇒ leave it alone, which is the safe direction on a shared account.

### One bug this surfaced in existing code
`reconciler._enrich_trades_pnl` groups fills by `exchange_order_id`. A super
order's three legs **share one orderId**, so when the stop fires its SELL fill
lands in the same bucket as the entry's BUY fill and the average blends the
two — a number describing no trade that ever happened, flowing into realized
P&L, the tax ledger and the EOD report. Fills are now matched on side as well
as order id, which is correct for every broker.

### Rollout
`attached_stops_enabled` defaults **OFF**, and deliberately so: there is no
usable Dhan sandbox for this endpoint, so the first real super order is also
its first execution. Every prior Dhan integration shipped unrehearsed has been
wrong on first contact — the `client-id` header (76/76 failures, months), the
MTF product on stops, the trigger outside the price band. This one is written
from the spec and 31 unit tests, and that is all it is until an entry proves
otherwise.

Enable for **one bucket**, watch the first entry, keep the sweep behind it.
That needs TWO gates, because a process-wide switch cannot express it — one
flip would arm swing-indian and intraday-indian in the same instant, on one
shared live account, unrehearsed. So `attached_stops: true` per bucket in
`buckets.yaml` is the rollout unit, and `attached_stops_enabled` stays as the
env-level master kill (switch it off on the VM without editing YAML). Both
must be true. Both default false.

**intraday-indian is the right pilot, not swing-indian.** Its positions square
off at 15:15, so a leg can never survive overnight — which removes the largest
unverified assumption in the whole design (whether a super order placed on
Monday is still listed, and its leg still resting, on Friday). Its stop is a
15% crash net deliberately far outside normal range, so the leg firing is a
tail event rather than the expected exit — which is what keeps the unbuilt
settlement half (below) off the critical path. swing-indian is the opposite on
both counts: it holds for days and its ATR stop is *meant* to fire.

**Verifiable offline:** body construction, the CNC fallback keeping its stop,
cancel-before-close ordering, fail-closed on cancel failure, ownership under a
missing correlationId, sweep coexistence, the invariant, and the trigger/target
arithmetic (the band clamp reproduces the live PIIND figure, 2288.20).
**Only verifiable live:** whether Dhan accepts the body, and whether
`correlationId` comes back on the legs.

### The settlement half — built 2026-08-17, and it fixes an older hole too

The first draft of this decision shipped placement only, and left a gap: when a
`STOP_LOSS_LEG` fires, the venue sells the stock but the bot sends no order, so
no `Trade` row is written. `net_owned` decrements only on a FILLED SELL row, so
the ledger would keep counting shares that are gone — and that ledger is the
ONLY thing separating the bot's stock from the user's on this shared account
(Decision 027). A permanent over-count means the next time the user buys the
same scrip, the bot treats part of THEIR holding as its own.

The obvious fix — ask the venue "did the leg fire" — needs `GET /v2/super/orders`,
whose cross-day retention is undocumented and unverifiable offline, and
swing-indian holds for days. That looked like a chicken-and-egg: the feature
could not ship without the answer, and the answer needed a live super order.

**It is not one, because the bot does not need to know WHAT sold the shares —
only that they are gone.** `Reconciler._detect_unrecorded_exits` compares the
bot's own ledger against the position data the sweep already fetches, and
writes the missing SELL row when the account holds less than the ledger claims.
No super-order endpoint, no unverified assumption.

Checking that premise turned up something better: **this hole is already open,
and predates super orders.** `_reconcile_positions` flips the `Position` row
FLAT when the exchange stops showing a position, but never writes a `Trade`
row — and the ownership maths reads `Trade`, not `Position`. So today, if the
user sells the bot's stock by hand, or Dhan's MIS auto-square-off closes an
intraday position, the ledger is already wrong. Super orders would not have
created this bug; they would have promoted it from rare to routine. One
mechanism now covers all three causes.

Two properties carry the safety:

- **A shortfall must survive three consecutive passes.** Not paranoia:
  `DhanClient.get_positions` fails SOFT on the holdings leg, so one errored
  `/v2/holdings` makes every settled swing holding briefly look sold. Acting on
  a single read would record a fictional exit and make the bot abandon a
  position it still holds. A changed size restarts the count — a position being
  sold down in pieces is still moving.
- **A price is taken only when today's SELL fills match the shortfall exactly.**
  On a shared account the trade book also carries the user's sells, and a
  partial match cannot be told from theirs. An exit with no price still
  corrects the ledger — the part that protects the user's stock — and simply
  does not pair for P&L. A fabricated price would corrupt realized P&L and the
  tax ledger permanently, which is worse than a gap somebody can see.

The row is deterministic and day-scoped, so re-running cannot duplicate it, and
it is self-limiting: once written, `net_owned` drops and the shortfall stops
being detected. `exchange_order_id` is prefixed `unrecorded:` so it can never
match an open-order set or a `get_order` lookup.

### Adversarial round on the exit ordering (2026-08-17)

**One blocker, found in this session's own code, reproduced and fixed.**

`plan_stop_protection`'s orphan-leg pass called a leg orphaned whenever the
broker reported no position behind it. But between placing a super order and
Dhan surfacing the position, **"leg with no position" is exactly what a
two-second-old entry looks like** — so the sweep would have cancelled the only
protection a brand-new position had. That is the same race that produced the
2026-08-11/12 attribution bugs, inverted into something worse: the pre-034 race
placed a duplicate stop, this one strips the real one.

A ledger check alone cannot fix it. `owned_quantities` counts an entry from
PENDING and decrements only on a FILLED sell, so for precisely the cases the
orphan pass exists for — Dhan's 15:20 auto-square-off, a manual close by the
user — it reports the symbol held forever and the leg would never be retired.
The guard has to be TIME-bounded: a symbol with a bot entry in the last
`_ENTRY_GRACE_MINUTES` (5) is never called orphaned. Both directions are pinned
by test.

**Accepted, not fixed:**

- *Partial entry fill vs leg size.* If Dhan sizes the STOP_LOSS_LEG to the
  REQUESTED quantity and the market entry only partly fills, the leg oversells
  on trigger. The old sweep compared stop size to position size; the attached
  branch cannot, because the leg's quantity is not something we set. Unverifiable
  offline — **watch the first partial fill.**
- *Same-minute retry after a blocked exit.* `client_order_id` is UNIQUE and the
  idempotent-hit set excludes CANCELED, so a second close attempt inside the
  same minute would raise `IntegrityError` rather than dedupe. Pre-existing —
  `_mark_rejected` has the identical shape — and out of reach for swing-indian
  (180s bucket tick). Noted because a misbehaving super-order API would hit the
  CANCELED path on *every* exit, which is the one scenario that makes it likely.
- *Three `GET /v2/super/orders` per sweep tick* (target retry, sweep, invariants),
  plus one per blocked close, on a rate-limited token shared with the user's
  manual trading. Ownership lookups are now process-cached (positive answers
  only — a negative is a statement about timing, not ownership). Fetching the
  list once per tick and passing it down is the remaining cleanup.

**Survived:** the chokepoint itself. Both close paths reach it, the
`reduce_only and stop_price is None` discriminator excludes protective-stop
placement, failure is fail-closed, and a crash between the leg DELETE and the
close self-heals on the next sweep (the symbol drops out of `attached_stops`,
so the normal path rests a standalone stop).

---

## 035 — GTT (Forever Orders) for overnight stop protection — PROPOSED, NOT BUILT
Date: 2026-08-18
Status: **Proposed. Nothing is built. Do not treat as implemented.**
Amends (if adopted): 022, for the multi-day Indian buckets only
Related house rules: #2, #8

Context: Decision 022 exists on one premise — "the stop rests ON the exchange,
so a max loss holds even when the bot or its VM is down." **For Dhan equity that
premise is false overnight, and has been since swing-indian went live on
2026-07-27.**

Two independent confirmations, found 2026-08-18 while checking whether a super
order's stop leg could survive multiple days:

1. `DhanClient._order_body` sends `"validity": "DAY"` on every protective stop.
   No inference needed — we *request* an order that expires at close.
2. The user's own hand-placed PIIND stop (order `1200000000373477`, accepted
   2026-08-14) was gone from the order book by 08-18.

So a swing-indian position carries no venue-resident stop between close and the
next morning's sweep.

### Sizing this honestly

A resting overnight stop would NOT have saved us from a gap: it triggers at the
open and fills at the gapped price, much like one re-placed at 09:15. The sweep
already re-places a missing stop each morning, so protection during market hours
is real.

The genuine exposure is narrower and worth stating plainly: **the bot or VM is
down at the open and the stock slides during that session.** No stop rests, and
nothing places one. That is precisely the scenario Decision 022 was written for,
and it is not covered.

Scope is the multi-day buckets only. intraday-indian squares off at 15:15, so
DAY validity is exactly right there and this decision does not apply to it.

### What GTT is, and why it fits

Dhan's **Forever Order** API (`/forever/orders`, with PUT/DELETE/GET siblings)
rests a trigger that outlives the session.

The load-bearing fact, quoted from the Dhan docs: the allowed
`productType` values are "`CNC` `MTF`" — which is exactly swing-indian's
product, and the reason this is worth pursuing at all rather than being blocked
the way MTF has blocked so much else.

It also carries the same leg vocabulary we already handle: `orderFlag` of
`SINGLE` or `OCO`, with `TARGET_LEG` / `STOP_LOSS_LEG` and a `triggerPrice`.
A protective stop is the `SINGLE` shape: SELL, `triggerPrice`, no target.

**One ambiguity, recorded rather than resolved:** the docs list `validity` as
`DAY`/`IOC` on the Forever Order too, which reads oddly for a product whose
point is persistence. The standard GTT design — the trigger rests indefinitely
and `validity` governs the child order placed WHEN it fires — reconciles it, but
that is a reading, not something the page states. Verify before building.

### The question that decides whether this is needed at all

`_super_order_body` sends **no `validity` field**, deliberately (it is limited to
documented fields), so Dhan's default applies to a super order's `STOP_LOSS_LEG`
and we do not know what that default is. Dhan's own line that you can place
"intraday, carry forward or even MTF orders via this order type" implies the
legs persist for a carry-forward product — suggestive, not proof.

So:

- **If a super-order SL leg survives overnight on MTF → this decision is
  unnecessary.** Super orders already give atomic protection at entry, which is
  strictly better than a separately-placed GTT.
- **If it does not → build this.** The two are then complementary: the super
  order closes the fill→stop gap during the session, the GTT is the overnight
  net.

**This is answered by observation, not by reasoning — watch swing-indian's first
MTF super order across a session boundary.** Guessing at Dhan's behaviour is how
the client-id header, the MTF product on stops, and the out-of-band trigger all
happened. Do not build this until that observation exists.

### If adopted, the known design constraints

- **One protective stop per symbol, always.** The sweep already enforces this
  against standalone stops and super-order legs (Decision 034); a GTT would be a
  third source and must join the same suppression, or a position ends up with
  two resting sells and the second one shorts.
- **Ownership.** A GTT must be provably ours before the bot ever cancels it —
  `correlationId` plus the ledger fallback, exactly as Decision 034 does, and for
  the same Decision 027 reason.
- **Retirement on exit.** A GTT that outlives its position is the naked-short
  hazard again, and worse than a super-order leg because it outlives the
  SESSION. It must retire through the same adapter chokepoint.
- **`_detect_unrecorded_exits` already covers the settlement side** — a GTT that
  fires while the bot is down writes no Trade row, and that mechanism records it
  from the position shortfall without needing to ask the venue.

## 036 — Two Indian F&O buckets: futures-indian and options-indian
Date: 2026-08-28
Status: **Phases A–D built. Nothing trades — neither bucket exists in buckets.yaml yet. Two gates remain open: the margin preflight has still never run against a live account, and the fee card is unsigned. Multi-leg structures are NOT supported; naked shorts and single legs are.**
Amends: 013 (the bucket set, again — this makes eight), 022 (stop semantics for
short premium), and CLAUDE.md's "Options: deferred until all futures/spot phases live"
Related house rules: #1, #2, #7, #8

Two new buckets on the existing Dhan account: `futures-indian` and
`options-indian`, built to crypto-longterm parity and shipping disabled.

### The four decisions taken up front (user, 2026-08-28)

1. **Capital: ₹5,00,000 each.** Not the ₹50k house standard, and the reason is
   arithmetic rather than ambition. SEBI's ₹15 lakh minimum contract value for
   index derivatives means one NIFTY lot (65) is ~₹15.8L of notional and
   ~₹1.9L of SPAN+exposure margin; BANKNIFTY (30) is ~₹2.1L. A ₹50k bucket
   cannot hold ONE futures lot or sell ONE option — only buy premium. At ₹2.5L
   a single short index lot is 76% of the bucket, which makes the scanner and
   both allocator caps inert. ₹5L is the smallest figure at which a ranked
   book means anything and a margin spike on a short leg has somewhere to go.
2. **`TradingType` gains FUTURES and OPTIONS.** These name an INSTRUMENT CLASS
   where every other value names a HOLDING PERIOD, and that wart was chosen
   with it understood: bucket ids parse as `<type>-<market>`, so this needs no
   change to id parsing, the `bucket_id` columns, the dashboard routes, or any
   existing bucket's identity. The alternative — a third (instrument) axis —
   is cleaner in the abstract and touches every one of those. The cost is that
   holding period is no longer expressible for a derivative bucket: one
   options bucket holds both intraday and swing option strategies, separated
   by their strategy_master rows and named scanner sets (Decision 026).
3. **Options: the full range, naked short premium included.** This is what
   forces a position-GROUP model, a mandatory SPAN margin preflight, and the
   dual-stop design below. It is the single largest driver of scope here.
4. **Universe: NSE index (5) + NSE stock F&O (228).** BSE (SENSEX, BANKEX) is
   explicitly out of v1 — a second exchange segment and a second expiry
   calendar, for underlyings we have no strategy for yet.

### What Phase A built (landed 2026-08-28)

`src/data_sources/dhan_fno.py` — an NSE derivative contract registry, separate
from `DhanData`'s equity universe so the memory-tuned equity parse is untouched.
Every number in it is read from Dhan's own scrip master;
`scripts/fno_registry_audit.py` re-measures all of them against a fresh
download and exits non-zero on drift, so "no guesswork in lot sizing" stays a
property of the system rather than a note about one afternoon.

**The finding that justifies the whole module: `SYMBOL_NAME` is not unique.**
It carries only the expiry MONTH, so `NIFTY-Sep2026-23150-CE` names FIVE
different weekly contracts (2026-09-01/08/15/22/29), each with its own
`SECURITY_ID` — 462 ambiguous names covering 2,236 NSE contracts. Keying on it
would silently trade the wrong expiry, which for a strategy with a
days-to-expiry rule is the worst available failure: it fills, it books, and
nothing downstream disagrees. The registry therefore mints its own symbol from
`(underlying, expiry, strike, option_type)` — verified unique across all 74,322
NSE rows — written as `NIFTY-20260908-23150-CE`. A collision in that map is
logged rather than silently shadowed, because a silent shadow is the exact bug
being avoided.

Three more measured properties the parse has to respect:

- **Futures carry sentinels, not nulls** — `OPTION_TYPE` is the literal `"XX"`
  and `STRIKE_PRICE` is `-0.01`. Both normalise to None.
- **Strikes are not always integers** — 1,654 NSE strikes are half-points, so
  the symbol renders the strike through `Decimal.normalize()`, never an int cast.
- **The segment is big** — NSE D is 74,322 of 197,254 rows, so widening the
  existing full-frame equity parse would add ~60MB on a 958MB VM that has
  already OOM'd once (2026-08-21). The F&O parse reads in chunks and filters
  per chunk, so peak is bounded by the chunk, not the segment.

**A latent bug this surfaced, now fixed.** `DhanClient._snap_tick` hardcoded a
₹0.05 grid. Tick size is per-contract and quoted in paise, and index futures do
NOT tick at ₹0.05: NIFTY and FINNIFTY read 10 (₹0.10), BANKNIFTY and NIFTYNXT50
read 20 (₹0.20), and 368 NSE contracts tick coarser than ₹0.05 — up to ₹5.00 on
12 stock futures. A ₹0.05-snapped price on a ₹0.10 grid is off-tick and refused,
so this would have rejected orders on the most liquid contracts in the market.
Harmless in cash equity (₹0.05 is a valid multiple of the ₹0.01 grid 1,463 NSE
names use), which is why it survived this long. `ContractSpec` on the broker
contract now carries lot / tick / freeze per instrument, injected the same way
`resolve_symbol` is, so the adapter still knows nothing about the scrip master
and every existing cash-equity caller behaves exactly as before.

One honest caveat, recorded because it is the only inferred number here: the
per-contract tick VALUE is read from the master, but the paise→rupee DIVISOR is
calibrated against NSE cash equity's known ₹0.05 reading as `5.0000`. If that
calibration is wrong every tick is wrong by exactly 100×, which the first live
order rejects loudly rather than filling badly.

Also landed: a freeze-quantity guard. NSE publishes a maximum quantity per
ORDER on every derivative (NIFTY: 1,756 = 27 lots). An oversized order is
REFUSED rather than clamped — a clamped entry silently opens a smaller position
than the allocator sized and than the stop was computed for, and a clamped exit
leaves a remainder open while every ledger row says flat.

### What Phase B built (landed 2026-08-29)

The seam where the signal and the instrument stop being the same thing. Every
bucket before these had one symbol flowing unchanged from scan to fill to exit;
here the scanner reasons about NIFTY and the order goes to one strike of one
expiry.

`src/shared/contracts.py` holds the symbol grammar, and it is a separate module
from the registry that mints symbols on purpose: the sizer dedups on it, the
reconciler will match on it, and the backtester replays it. A grammar duplicated
across four modules is a grammar that will disagree in three of them.

**The bug that shaped it.** `underlying_of` is the dedup key, and the obvious
implementation — `symbol.split("-")[0]` — is wrong on a name this system trades
with real money today: swing-indian's universe contains `NAM-INDIA`, which that
shortcut turns into `NAM`. The grammar therefore anchors on the 8-digit expiry
and matches the underlying greedily, and returns any non-matching symbol
unchanged. That last property is what lets every caller use it unconditionally:
for cash equity and crypto it is the identity function, so there is no F&O
branch anyone can forget.

`src/shared/contract_selection.py` is the rule engine, configured by a
`contracts.yaml` (or `contracts_<name>.yaml`, Decision 026 shaped) in the bucket
folder. Strike rules: ATM, OTM%, ITM%, OTM-steps. Expiry rules: nearest, weekly,
monthly, with min and max days-to-expiry. It needs **no new API call** —
everything resolves against the scrip master the registry already holds plus a
spot price the runner already fetches, which matters against a rate-limited
single-session account.

Four choices in it worth recording:

- **`delta` is refused at config load, not silently downgraded to ATM.** Nothing
  in this repo fetches greeks. A 0.30-delta short strangle sized as though it
  were ATM is a different trade with a different loss profile, so a config
  asking for it fails loudly.
- **Weekly falls back to monthly where no weeklies list.** NSE now lists
  weeklies for NIFTY only. Without the fallback a weekly-configured strategy
  would trade nothing on 232 of 233 underlyings, and would do it silently.
- **Every tie-break is total.** Nearest-strike ties go to the LOWER strike and
  the chain is re-sorted rather than trusted, because index ladders are evenly
  spaced so an exact midpoint is routine, and "whichever the sort yielded" would
  make one signal pick different contracts on different runs. Determinism is not
  a nicety here: without it a live fill cannot be compared against the backtest
  that justified it.
- **An off-ladder OTM-steps request is a MISS, not a clamp.** Clamping to the
  last listed strike returns a contract at a completely different moneyness than
  the config asked for.

**Dedup now keys on the underlying** (`dedup_keys()` in the sizer), live for
every bucket. This is the item that would have been quietly catastrophic: the
ledger holds contract symbols and the scanner offers underlyings, so a gate
comparing the two never matches, and a strategy already short one NIFTY strike
reads a fresh NIFTY signal as an unrelated name and opens a second — doubling
exposure `per_symbol_cap` believes it has capped. It is extracted as a named
function rather than left inline because nothing in the suite calls
`size_positions` (the tests deliberately keep the DB out), and an untested
inline set-build is exactly where this regresses.

`contract_hint()` builds the audit payload — contract, underlying, expiry,
strike, leg, lot — riding the existing `hint` → `_entry_extra` → `Trade.extra`
JSONB path that already carries `stop_distance` and `signal_price`. No migration.
Without it, a fill on `NIFTY-20260908-23150-CE` cannot be traced back to the rule
that chose it, and "why that strike?" becomes unanswerable after the fact.

**Deferred to Phase C, deliberately:** threading the execution symbol through the
runner (two price fetches per candidate — spot for the strike, premium for the
size) and onto the order. It is inseparable from lot quantisation, and a runner
that selects a contract but still sizes in shares would be worse than one that
does neither.

### What Phase C built (landed 2026-08-29)

**A lot is a minimum, not a rounding step.** `quantize_to_lots` floors onto the
venue's grid and never rounds up. The asymmetry is the point: one NIFTY lot is
~₹15.8 lakh of notional against a ₹5L bucket, so rounding a short size up to
"the minimum tradeable" would place an order three times what the allocator
approved. It runs AFTER `_fit_to_margin`, because a quantity scaled to the
margin actually granted lands off the grid nearly every time. The sizer counts
LOTS (contract_size = lot size), which makes the pre-existing `size < 1` guard
mean "less than one lot" without a new branch.

**The margin preflight is now mandatory for a derivative.** For cash equity,
`required_margin()` returning None degrades to a 1× fallback, and that is
sound: margin there is a leverage multiple of notional, so 1× is a quantity we
can always afford. A derivative has no 1×. SPAN plus exposure is the exchange's
risk model applied to the UNDERLYING's notional — one NIFTY lot is ~₹15.8L of
exposure carrying ~₹1.9L of margin, and no fraction of that is "unleveraged".
Sizing off a leverage guess would submit an order the venue prices at multiples
of the budget, and on a short option there is no bounded loss behind the
mistake. So no margin answer means no order, which is what makes Phase C's gate
"the preflight answers correctly against a live account" — a method this repo
has never once exercised on one.

**The execution symbol flows end to end** (the piece deliberately deferred from
Phase B). `ExecutionPlan` carries contract symbol, premium and lot size keyed by
UNDERLYING, so the scanner, the regime model and the dedup gate keep seeing the
underlying while the order goes to the contract. For a bucket with no
`contracts.yaml` it is a pass-through of the same objects, so there is no second
code path to keep in step.

### The fee card, and what reconciling it found

Rates were fetched and CROSS-CHECKED against two independent sources per F&O
line, not recalled. Two things that would have been wrong from memory:

- **F&O STT moved on 1 April 2026** (Budget 2026-27): futures 0.02% → 0.05%,
  options 0.10% → 0.15% on premium, both sell-side. Anyone working from a
  pre-April figure understates an option round trip by a third.
- **The sources disagree on futures stamp duty** — 0.002% vs 0.0001%. Recorded
  in that line's note rather than smoothed over, with the more authoritative
  figure used and flagged as the first line to check.

`estimate_charges` REFUSES to run against an unsigned card. Not a warning and
not a zero: an estimate silently returning zero reads downstream as "this trade
is free", which is worse than having no estimate.

Then the card was replayed against charges Dhan has ALREADY billed —
`scripts/fee_card_reconcile.py`. That is possible before either F&O bucket
exists because the cash buckets have been trading this same account for months.
**Four of six lines reconcile to ~0%: brokerage, exchange transaction, SEBI,
GST. STT does not.** Two buy legs were billed ₹12 on ~₹50k turnover (~0.024%),
which matches neither the intraday rate (0.025%, sell side only) nor delivery
(0.1%, both sides), while the one clean same-day round trip in the sample was
billed ₹0 exactly as the card predicts. I could not account for it from the
cited sources and did not construct an explanation; the card ships unsigned with
the finding in its own header.

Two further limits worth stating plainly: every reconciled order was a BUY, so
the sell-side STT lines — the largest cost in an F&O round trip — have never
been checked against a real bill; and stamp-duty actuals arrive as whole rupees
against fractional estimates, which looks like a rounding convention rather than
a wrong rate.

That reconciliation also exposed a hole worth fixing on its own account: the
product an order was sent as was never recorded, so cost attribution had to be
guessed from the bucket — and that guess is wrong precisely when the Decision
031 CNC fallback fires, which is the case a cost check most needs to get right.
`Trade.extra["product"]` now records it.

**Deferred on purpose:** the automated drift alert inside the reconciler. An
alert that is both inert (the card is unsigned) and unvalidated (STT is
unexplained) would be worse than none. It lands with sign-off.

### What Phase D built (landed 2026-08-29)

Two hazards turned up in code that has been live on real money for months. Both
were invisible while every Indian position was long, and both would have bitten
on the first F&O order.

**A naked short was not recognised as the bot's own.** `net_owned` counts BUY as
positive and SELL as negative and keeps only positive nets — correct for a
long-only world, and exactly wrong for a bucket that OPENS with a sell. A sold
option nets negative, drops out of ownership, and every safety path then reads
it as the user's position: the stop sweep skips it, the reconciler declines to
adopt it, the breaker flatten passes over it. A position with unbounded loss
that nothing believes it owns is the worst thing this system could hold.

`net_owned_signed` is the fix, with `net_owned` derived from it rather than
computed separately — two implementations of "what do we own" is precisely how
`_load_attribution` and `_load_stop_distances` drifted apart and cost a live
position its stop. The DB wrapper gained `signed=`, which mattered more than it
looks: without threading it through, the signed function would have existed and
every caller would still have used the blind view.

**The stop-coverage invariant hardcoded that a protective stop is a SELL.** True
of every Indian position until now. A short is protected by a BUY, and a resting
SELL against a short *adds to it*. The check now keys on the sign of the holding.

A third, subtler one surfaced from a failing test rather than from reading:
`reduce_only` was stamped only when True, so an opening SELL was
indistinguishable from a row written before the flag existed, and fell back to
the long-only rule that a SELL closes. The bot would have been blind to its own
naked short in the window between placement and fill — which is the exact window
the early-recognition rule exists to cover. It is now stamped on every order,
both values.

### The expiry window — the blocker this phase existed to clear

`check_expiry_window` refuses to let any derivative be held into its expiry
window. Stock F&O is PHYSICALLY SETTLED: an in-the-money contract carried past
expiry does not settle in cash, it delivers shares at the full contract value —
roughly ₹6.7 lakh for a median NSE lot against a ₹5 lakh bucket — and the
shortfall goes to auction. Index F&O settles in rupees and gets a looser floor.

**Anything not declared cash-settled is treated as physically settled.** The
fail-safe direction is deliberate: an underlying missing from the index set
costs an early square-off, while the opposite mistake costs a delivery
obligation the bucket cannot fund.

Like every invariant it can only HALT (Decision 033). Engaging the kill switch
stops the bucket adding to the problem; closing the position is a trading
decision and stays with the strategy or a human, and the alert says so.

`check_margin_utilisation` covers the other way a short bucket dies quietly. A
derivative's margin is the exchange's risk model re-evaluated continuously, and
on a short option it GROWS as the position moves against you. Unwatched, the
first the system hears is the broker squaring off at the worst available price.
The balance call it needs is made only when a bucket on that account actually
trades derivatives, so cash-equity accounts add no request to a rate-limited
API.

One config change, small but load-bearing: `stop_loss_pct` was bounded `lt=100`.
That is right for a long — a price cannot fall more than 100% — and wrong for a
short, where the same field is a PREMIUM MULTIPLE and 100 means "close when the
premium doubles". The existing stop arithmetic already places a short's trigger
above entry, so no new mechanism was needed; the bound and the documentation
were the whole gap.

### What Phase D deliberately did NOT build

**Position groups.** Multi-leg structures need a strategy to shape the
interface — how legs pair, which is the risk leg, what a partial fill on one leg
implies for the other. Building that against no strategy would be guessing.
Naked shorts and single legs are fully handled; **a spread is not, and must not
be traded until this lands.**

**The bot-side underlying-level exit.** The exchange-resident half of the dual
stop works today: a premium-multiple stop resting at the venue, which is the
half that still bounds the loss when the VM is dead. The underlying-level half
belongs in a strategy's `select_exits` (Decision 021), and there is no strategy
yet.

### Still to build, in dependency order
- **Phase E — buckets, configs, dashboard, docs.** Gated on the user's backtest
  handoff; both buckets ship `enabled: false` until then.

### The blocker Phase D must clear: physical settlement

Stock derivatives are physically settled. An ITM stock option or future carried
past expiry delivers SHARES at full contract value — a ₹6.7L median obligation
against a ₹5L bucket, i.e. margin shortfall and auction. Index derivatives are
cash settled and carry no such risk. `DerivativeContract.physically_settled`
already carries the distinction because it is a property of the instrument, not
of a strategy; Phase D turns it into a mandatory pre-expiry square-off enforced
as a session invariant (Decision 033), never left to a strategy to remember.

### Unverified, and to be verified rather than assumed

- `required_margin()` has still never run against a live account.
- Super Order (Decision 034) is proven on cash equity only. If F&O refuses an
  attached stop, the Decision 022 sweep is the fallback and the dual-stop
  design changes shape.
- The carry-forward product string is probably `MARGIN`, but the adapter passes
  the string through untouched, so it gets confirmed against Dhan's docs and a
  sandbox order.
- Dhan's F&O `tradingSymbol` format is unknown, which is why the registry keeps
  a `by_security_id` reverse index — `securityId` is on every payload and is
  unique, so the reconciler can match whatever the string turns out to be.
- `NSE_FNO` as the order-side segment name comes from Dhan's API docs, not from
  the master. It is a module constant, so it is one edit if wrong.
