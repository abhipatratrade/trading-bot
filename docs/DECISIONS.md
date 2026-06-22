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
