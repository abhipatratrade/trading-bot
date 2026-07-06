# PHASES.md — Build Tracker

Tick boxes as items complete. The next session reads this file to know
where to resume. Always commit updates to this file before ending a session.

Legend: `[ ]` pending · `[~]` in progress · `[x]` done · `[!]` blocked (see notes)

---

## Phase 0 — Foundations

Goal: a runnable but not-yet-trading skeleton with all common plumbing and
continuity files in place. No live orders. No real strategy yet.

### 0.1 Repo & continuity
- [x] Folder structure created
- [x] `CLAUDE.md` — bible pointer
- [x] `docs/PHASES.md` — this file
- [x] `docs/DECISIONS.md` — locked architecture decisions
- [x] `pyproject.toml` (Python ≥ 3.11, deps pinned)
- [x] `.gitignore`
- [x] `.env.example`
- [x] `railway.toml` — service definitions
- [x] `README.md`
- [x] Initial git commit

### 0.2 Core plumbing
- [x] `src/core/config.py` — pydantic Settings, env-driven, TRADING_MODE switch
- [x] `src/core/logging.py` — structured JSON logs + secret redaction filter
- [x] `src/core/db.py` — SQLAlchemy engine + session factory
- [x] `src/core/models.py` — `Trade`, `Position`, `AuditLog`, `KillSwitch`,
      `StrategyParamChange`, `DailyUniverse`, `ScannerSnapshot`,
      `SymbolMapping`
- [x] `src/core/clock.py` — injectable clock (real / fake) for tests
- [x] Alembic init + first migration (`0001_initial_schema`)

### 0.3 Broker layer (testnet only in this phase)
- [x] `src/brokers/base.py` — `Broker` ABC (place_order, cancel, positions, balances)
- [x] `src/brokers/delta_india/client.py` — REST + HMAC signing (testnet)
- [x] `src/brokers/delta_india/ws.py` — WebSocket: positions, fills, ticker
- [x] Smoke script: place + cancel a testnet order from CLI

### 0.4 Data sources
- [x] `src/data_sources/base.py` — `MarketData` interface
- [x] `src/data_sources/binance.py` — public WS + REST (no auth)
- [x] `src/data_sources/delta_india.py` — public market data
- [x] Symbol mapping loader (CSV → `symbol_mapping` table)

### 0.5 Order manager + reconciler
- [x] `src/order_manager/manager.py` — idempotent placement (`client_order_id`)
- [x] `src/order_manager/reconciler.py` — DB ↔ exchange diff at startup + every 5 min

### 0.6 Safety
- [x] `src/safety/kill_switch.py` — DB-flag check, called every loop
- [x] `src/safety/breakers.py` — daily DD, liquidation distance, funding extreme
- [x] Dashboard kill-switch button writes to DB (built in 0.7)

### 0.7 Dashboard skeleton
- [x] `src/dashboard/app.py` — FastAPI + HTMX shell
- [x] Pages: positions, recent trades, kill switch, params snapshot, CSV export

### 0.8 Scheduler + nightly export
- [x] `src/entrypoints/run_scheduler.py`
- [x] Nightly job: dump trades to Parquet + CSV → Google Drive folder
- [x] Telegram alert wiring (env-gated, no-op if no token)

### 0.9 Railway provisioning (USER does this part interactively)
- [x] User: create Railway project
- [x] User: provision Postgres
- [x] User: set env vars (DELTA_TESTNET_*, BINANCE_PUBLIC_*, KITE_*, TELEGRAM_*, GDRIVE_*)
- [x] Deploy 3 services: bot-worker, dashboard, scheduler
- [x] Verify all 3 boot, dashboard reachable, kill switch flippable

**Phase 0 exit criterion**: bot-worker boots on testnet, reconciles cleanly,
dashboard shows kill switch, scheduler runs nightly export. **No real strategy yet.**

---

## Phase 1 — Crypto Long-term [priority 1]

Strategy: 1D timeframe, 5x leverage, top-5 by Delta India 24h volume,
Kelly-sized against ₹50k bucket capital, optional regime multiplier.

### 1a — Bucket framework (Decisions 013-017)
- [x] `buckets.yaml` at repo root
- [x] Migration 0002 — new tables + column adds + seed bucket_state
- [x] `src/shared/`: bucket, base_strategy, strategy_loader,
      strategy_master (schema+loader), scanner engine, allocator
      (kelly+caps+sizer), regime (features+HMM+regimes+store+brain+retrain),
      bucket_runner
- [x] Six bucket folders with yaml/csv stubs (`longterm-crypto` fully
      populated; others disabled stubs)
- [x] `top5_volume.py` Strategy subclass under `longterm/crypto/strategies/`
- [x] Old `src/strategies/crypto_longterm/` + legacy volume_scanner removed
- [x] `run_bot.py` iterates all enabled buckets each tick
- [x] `run_scheduler.py` registers per-bucket regime retrain jobs
- [x] Dashboard: `/buckets` 6-card overview + per-bucket detail page
- [x] Unit tests for shared modules (36 passing)

### 1b — Soak restart on new structure (Decision 017)
- [ ] Run migration 0002 against GCP Postgres
- [ ] Redeploy bot to GCP VM
- [ ] **Run on testnet ≥ 14 days unattended on the new structure**
- [ ] Train initial HMM on BTCUSDT 1D and flip `regime.enabled: true` in
      `longterm/crypto/regime.yaml`
- [ ] Update `allocator.yaml` μ/σ values from backtester output + new
      `backtest_ref`
- [ ] Go live with ₹50,000 capital

**Phase 1 exit criterion**: 14 testnet days clean (under new structure) +
first live week clean.

### 1c — Review backlog (2026-07-06 critical review + user asks)

Source: full-project review session 2026-07-06 (Decision 021) + user
requirements from the same conversation. On `continue`, work these
top-to-bottom.

**Shipped in commit c36b038 (2026-07-06):**
- [x] Exit engine — BucketRunner step 0 calls `select_exits` per strategy
      (incl. gate-blocked ones); reduce-only closes; top5_volume exits on
      regime→BEAR, ema_9_15 exits on EMA(9)<EMA(15)
- [x] Breaker enforcement — trip → per-bucket kill switch + flatten
      (`src/safety/enforcement.py`, called per tick per sub-account)
- [x] Capital truth — reconciler mirrors sub-account wallet into
      `bucket_state` (available/locked × allocator fx) every sweep
- [x] Dashboard HTTP basic auth (`DASHBOARD_USER`/`DASHBOARD_PASSWORD`)
      + traceback leak removed
- [x] Order safety — transport-error recovery via client_order_id lookup
      (no double-fire), `set_leverage` failure aborts placement,
      `EntryCandidate.side` honored, status maps handle partial/rejected
- [x] Regime hygiene — inference + retrain drop the in-progress candle

**Left (priority order):**
- [ ] **Dashboard: cumulative bucket P&L** — on `/buckets` cards and the
      bucket detail page, show cumulative P&L amount and % **between the
      Available-balance and Regime sections**. Basis: wallet equity
      (available + locked, already synced) minus `capital_inr`, % vs
      `capital_inr`. Since each sub-account is bot-exclusive (Decision
      019), wallet delta = bot P&L incl. fees/funding. Caveat to handle:
      manual deposits/withdrawals on the sub-account skew it — document
      or track a deposits offset in `bucket_state.extra`.
- [ ] **Fills/fees/realized-P&L ingestion** (prerequisite for per-trade
      P&L) — poll Delta `GET /v2/fills` (or use the orphaned WS client)
      during the reconciler sweep; store fill price + commission on
      `Trade`; compute realized P&L on close.
- [ ] **Dashboard: per-trade traded amount + P&L amt & %** — for each
      trade row: traded notional (fill price × contracts × contract
      size), live P&L amount and % (unrealized via mark vs entry while
      open; realized once closed). Refresh every reconcile sweep (5 min);
      drop to per-tick (60s) if Delta rate limits allow — positions are
      already fetched per tick by the breakers, so reuse that call.
- [ ] Broker-side stop-loss order placed with every entry (protection
      when bot/VM is down) — biggest remaining risk gap
- [ ] Daily-anchored drawdown breaker — start-of-day equity snapshot per
      account (realized + unrealized vs anchor; needs small migration)
- [ ] Heartbeat / dead-man's switch — bot writes a heartbeat row; Railway
      scheduler (or healthchecks.io) alerts on silence
- [ ] Per-bucket tick cadence derived from bucket TF (stop re-scanning a
      1d bucket every 60s) + retention/pruning job for scanner_snapshot /
      sizing_snapshot / audit_log
- [ ] Dedup window scaled to strategy TF (replace hardcoded 23h before
      Phase 2 swing goes live)
- [ ] Contract sizes + FX from live sources — read `contract_value` from
      Delta `/v2/products` instead of YAML; periodic USD/INR refresh
- [ ] Delta client hardening — retry/backoff, HTTP 429 handling, clock-skew
      tolerance on HMAC timestamp, periodic product-catalogue refresh
- [ ] Regime brain cache for 4h/15m TFs (currently uncached → per-tick HMM)
- [ ] Kill-switch semantics refinement (user to confirm): allow strategy
      exits and/or breaker watch while manually killed (reduce-only paths)
- [ ] CSRF token on kill-switch toggle (basic auth mitigates; cheap to add)
- [ ] Config cleanup — drop `kite_*` settings, add `dhan_*` (Phase 3 prep)

---

## Phase 2 — Crypto Swing [priority 2]
*(Detailed checklist added when Phase 1 completes.)*

## Phase 3 — Stocks Long-term integration [priority 3]
*(Detailed checklist added when Phase 2 completes. Wraps existing Kite system.)*

## Phase 4 — Stocks Swing [priority 4]
*(Detailed checklist added when Phase 3 completes. Activates Portfolio Allocator.)*

## Phase 5 — Crypto Scalp [priority 5]
*(Detailed checklist added when Phase 4 completes. Latency-tuned path.)*

## Phase 6 — Crypto Gambling [priority 6]
*(Detailed checklist added when Phase 5 completes. Memecoin pump scanner.)*

## Phase 7+ — Agentic perimeter
*(Postmortem, research, news, param tuner — advisory only, separate from core.)*

## Phase 8+ — Options
*(Deferred per Goal_Setting.txt priority [10]/[11].)*

---

## Session Log

Append a one-liner per session for traceability.

- 2026-04-30 — Phase 0 kicked off: scaffold + continuity files written.
- 2026-04-30 — Phase 0.2 done: core plumbing (config, logging, db, models, clock) + Alembic + initial migration.
- 2026-05-01 — Phase 0.3 done: Broker ABC, Delta India REST+WS client, smoke test passed on testnet (place+cancel+balances+positions all verified).
- 2026-05-01 — Phase 0.4 done: MarketData ABC, Binance REST+WS, Delta India public data, symbol mapping sync (9 overlapping perps on testnet). CSV at data/symbol_mapping.csv.
- 2026-05-01 — Phase 0.5 done: OrderManager (idempotent placement, kill switch, audit logging) + Reconciler (position/order sync, orphan detection). Broker.get_order added.
- 2026-05-01 — Phase 0.6 done: kill_switch.py (engage/disengage/is_engaged) + breakers.py (daily DD, liq distance, funding extreme).
- 2026-05-01 — Phase 0.7 done: FastAPI+HTMX dashboard (positions, trades, kill switch toggle, params snapshot, CSV export). Dark theme.
- 2026-05-01 — Phase 0.8 done: APScheduler entrypoint, nightly Parquet+CSV export, GDrive upload, Telegram alerts (env-gated).
- 2026-05-01 — Phase 0.9 done: Railway deployed — dashboard live at dashboard-production-71e0.up.railway.app, scheduler online, Postgres migrated. Phase 0 COMPLETE.
- 2026-05-02 — Phase 1 code complete: policy.yaml + schema validator, volume scanner (top-5 by Delta 24h volume, Binance-filtered), daily rebalance runner (close exits → open entries at equal-weight × 5x), strategy-specific breaker wrapper, bot entrypoint with startup reconciliation + symbol mapping refresh. Ready for testnet deployment.
- 2026-05-03 — Bot deployed to GCP VM (35.184.66.247, static IP, e2-micro, us-central1-f). Railway bot-worker deleted. First testnet rebalance ran: opened BTCUSD, ETHUSD, SOLUSD, XRPUSD. Telegram alerts working. 14-day testnet soak started. Decision 012: switched stocks broker from Zerodha to Dhan (free API, 30-day tokens).
- 2026-06-10 — Major restructure per PPTX `C:\Users\User\Documents\Trading bot instructions.pptx`: six (type × market) buckets, per-bucket regime HMM, Kelly sizer with insufficient-balance skip rule, CSV Strategy Master, dashboard 6-card overview + per-bucket pages. Decisions 013-017 added. Old `crypto_longterm` removed and re-ported as `longterm/crypto/strategies/top5_volume.py`. 36 unit tests passing. Soak clock to restart at next deploy.
- 2026-06-10 — Added EMA 9/15 crossover strategy for swing-crypto bucket. Populated `swing/crypto/allocator.yaml` with industry-standard μ/σ (BTC annualized 40%/70% → 1H mu=4.6e-5 sigma=0.0075; 10 majors total). 7 EMA strategy tests + scripts/swing_crypto_dryrun.py end-to-end check passing (3 candidates placed, ₹24.9k margin / ₹249.9k notional within ₹50k bucket at 10x leverage). Decision 018 added: sizer insufficiency check uses required margin, not leveraged notional.
- 2026-06-12 — Deployed restructure to prod. Ran migration 0002 on Railway Postgres (4 new tables, 6 bucket_state rows seeded). git push origin main triggered Railway dashboard + scheduler auto-deploy. GCP VM bot-worker.service: git pull, pip install hmmlearn+scipy, systemctl restart → active, BucketRunner now driving longterm-crypto with top5_volume. Hit psycopg2 InvalidTextRepresentation on audit_log writes because SAEnum serialises Python member NAMES (uppercase) while migration 0002 added new values in lowercase; fixed via manual ALTER TYPE on prod + migration 0003 (UPPERCASE versions) committed for fresh-install correctness. Railway dashboard verified serving new /buckets routes. 43 unit tests still green.
- 2026-07-06 (later) — Phase 1c backlog section added: review leftovers + two new user asks (cumulative bucket P&L on dashboard; per-trade traded amt + P&L amt/% refreshed ≤5 min, with fills-ingestion prerequisite). `continue` resumes from Phase 1c top item. Deployed c36b038 to VM + Railway; DASHBOARD_PASSWORD set on Railway dashboard service.
- 2026-07-06 — Critical-review session → Decision 021 shipped: exit engine wired into BucketRunner (step 0: `select_exits` per strategy incl. gated ones; top5_volume exits on BEAR flip, ema_9_15 on EMA state-down), breakers enforced per tick (trip → per-bucket kill switch + reduce-only flatten via `safety/enforcement.py`), reconciler now mirrors sub-account wallet into bucket_state (available/locked × allocator fx), dashboard HTTP basic auth (DASHBOARD_USER/PASSWORD) + traceback leak removed, OrderManager transport-error recovery via client_order_id lookup (no double-fire), set_leverage failure aborts placement, EntryCandidate.side honored (shorts plumbing), regime inference+retrain drop the in-progress candle, status maps gained partial/rejected. 117 unit tests green (was 73).
- 2026-06-15 — Bot-worker VM migrated us-central1-f → asia-south1-a (Mumbai). Binance HTTP 451 geoblock cleared (no more Delta-only fallback for OHLCV). New VM `trading-bot-worker-mumbai`, static IP 34.14.200.220, whitelisted on Delta India. Old VM `trading-bot-worker` (35.184.66.247) systemd service stopped + disabled; VM kept for 24h soak, to be deleted ~2026-06-22. Per-coin HMM Brain (migration 0005 + symbol-keyed model+snapshot) shipped same day: tiered training (3-state-full / 3-state-diag / 2-state-diag / skip), `_market_` fallback under MARKET_SENTINEL, per-symbol regime dict in BucketRunner→sizer, dashboard regime grid per symbol. `/params` route split into index + allocation + trading + scanner sub-pages. Retrain flipped back to Binance (richer history via symbol_mapping Delta↔Binance crosswalk). 73 unit tests green.
