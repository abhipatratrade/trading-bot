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
- [x] **Dashboard: cumulative bucket P&L** — shipped 2026-07-07: P&L amt
      + % (vs capital) shown between Available and Regime on `/buckets`
      cards and bucket detail. Basis: synced wallet equity −
      `capital_inr` − optional `bucket_state.extra["capital_adjustments_inr"]`
      (manual deposit/withdrawal offset). Math in
      `src/order_manager/pnl.py::bucket_cumulative_pnl`.
- [x] **Fills/fees/realized-P&L ingestion** — shipped 2026-07-07:
      `Broker.get_fills` + Delta `GET /v2/fills` (µs epoch start_time,
      page_size 500); reconciler `_enrich_trades_pnl` sweep stores avg
      fill price / fees / traded notional on `Trade` and pairs
      reduce-only exits with entries for realized P&L (net of both
      sides' fees). Contract size now read live from `/v2/products`
      `contract_value`.
- [x] **Dashboard: per-trade traded amount + P&L amt & %** — shipped
      2026-07-07: trade rows (bucket page + home) show fill price,
      traded notional, P&L amt + % with realized/unrealized tag,
      refreshed by the 5-min reconcile sweep. P&L % is against traded
      notional (× leverage = margin-relative). Follow-up if wanted:
      per-tick (60s) refresh by reusing the breakers' positions call.
- [x] **Broker-side stop-loss on every position** — shipped 2026-07-07
      (Decision 022): per-tick stop-protection sweep
      (`src/safety/stop_protection.py`) keeps an exchange-resident
      reduce-only stop-market order on every open position at
      `stop_loss_pct` from entry (`buckets.yaml`, seeded at 50% of
      margin at leverage_max; longterm-crypto = 10%). Self-healing:
      places missing stops, resizes on adds, cancels orphans. Trigger =
      mark price, snapped to live tick_size. Stops flow through
      OrderManager so a stop that fires while the bot is down still gets
      P&L-paired by the reconciler.
- [x] **Daily-anchored drawdown breaker** — shipped 2026-07-07 (Decision
      023): `daily_equity_anchor` table (migration 0007, applied to
      Postgres) snapshots each sub-account's equity (wallet + unrealized)
      at first breaker pass of the UTC day; `check_daily_drawdown` is now
      pure math vs that anchor, so realized losses count. Bonus:
      `ops/deploy.sh` auto-runs `alembic upgrade head` when a push
      touches `migrations/`.
- [x] **Heartbeat / dead-man's switch** — shipped 2026-07-07: bot-worker
      upserts a `heartbeat` row after every completed tick (migration
      0008, applied); Railway scheduler job checks age every 2 min and
      pages when older than `HEARTBEAT_STALE_SECONDS` (default 600),
      with hourly-capped dedup + one-off recovery ping. Watchdog runs on
      Railway, so a dead VM can't silence it.
- [x] **Per-bucket tick cadence + retention pruning** — shipped
      2026-07-07: pipeline passes paced to bucket TF (tf/20 clamped to
      [60s, 15min]: 1d → 900s, 1h → 180s, ≤15m → 60s) via
      `tick_interval_for_tf`; breakers/stop-sweep/heartbeat stay on the
      60s loop. Nightly scheduler prune job (01:00 UTC) deletes
      scanner/sizing/regime snapshots older than
      `SNAPSHOT_RETENTION_DAYS` (60) and audit_log older than
      `AUDIT_RETENTION_DAYS` (180).
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
- 2026-07-07 (cont.) — Phase 1c item 7 shipped: per-bucket tick cadence (`tick_interval_for_tf`, 1d bucket now re-scans every 15 min instead of 60s; safety paths unchanged at 60s; crashing buckets back off to their cadence) + nightly retention prune on Railway scheduler (`src/core/retention.py`; snapshots 60d, audit_log 180d). 165 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 6 shipped: heartbeat/dead-man's switch. `heartbeat` table (migration 0008), `src/core/heartbeat.py` (beat/last_beat/pure staleness), bot beats after each completed tick, Railway scheduler `heartbeat_watch` job (2 min interval) pages on stale (>600s, HEARTBEAT_STALE_SECONDS) with dedup + recovery ping. 160 unit tests green.
- 2026-07-07 (later still) — Phase 1c item 5 shipped (Decision 023): daily-anchored drawdown breaker. `daily_equity_anchor` table (migration 0007 — applied to Railway Postgres this session), first breaker pass of each UTC day snapshots account equity (wallet + unrealized), `check_daily_drawdown` rewritten as pure anchor-vs-current math so realized losses count toward the 5% trip. `ops/deploy.sh` now auto-applies alembic migrations on pushes touching `migrations/`. 155 unit tests green (was 147).
- 2026-07-07 (later) — Phase 1c item 4 shipped (Decision 022): broker-side protective stop-losses. New `src/safety/stop_protection.py` sweep (per tick per sub-account + startup) rests a reduce-only stop-market order on every open position at `stop_loss_pct` from entry (buckets.yaml; 0.5/leverage rule). Delta client gained stop-order placement (stop_loss_order/mark_price), `states=open,pending` on get_open_orders (untriggered stops), and live tick_size; OrderManager gained stop_price + "STOP" alerts; protective stops excluded from exit-engine dedup. 147 unit tests green (was 131).
- 2026-07-07 — Phase 1c items 1-3 shipped: cumulative bucket P&L (wallet equity vs capital, with capital-adjustments offset) on /buckets cards + detail page; fills/fees ingestion (Delta /v2/fills → Trade.fees + extra: avg_fill_price/traded_notional_usd; realized P&L by exit↔entry pairing; unrealized from exchange positions) in a new reconciler `_enrich_trades_pnl` sweep step; per-trade Traded amt / P&L / P&L% columns on bucket + home trade tables. Contract sizes now read from Delta /v2/products. New pure-math module src/order_manager/pnl.py + 14 tests (131 total green).
- 2026-07-06 (later) — Phase 1c backlog section added: review leftovers + two new user asks (cumulative bucket P&L on dashboard; per-trade traded amt + P&L amt/% refreshed ≤5 min, with fills-ingestion prerequisite). `continue` resumes from Phase 1c top item. Deployed c36b038 to VM + Railway; DASHBOARD_PASSWORD set on Railway dashboard service.
- 2026-07-06 — Critical-review session → Decision 021 shipped: exit engine wired into BucketRunner (step 0: `select_exits` per strategy incl. gated ones; top5_volume exits on BEAR flip, ema_9_15 on EMA state-down), breakers enforced per tick (trip → per-bucket kill switch + reduce-only flatten via `safety/enforcement.py`), reconciler now mirrors sub-account wallet into bucket_state (available/locked × allocator fx), dashboard HTTP basic auth (DASHBOARD_USER/PASSWORD) + traceback leak removed, OrderManager transport-error recovery via client_order_id lookup (no double-fire), set_leverage failure aborts placement, EntryCandidate.side honored (shorts plumbing), regime inference+retrain drop the in-progress candle, status maps gained partial/rejected. 117 unit tests green (was 73).
- 2026-06-15 — Bot-worker VM migrated us-central1-f → asia-south1-a (Mumbai). Binance HTTP 451 geoblock cleared (no more Delta-only fallback for OHLCV). New VM `trading-bot-worker-mumbai`, static IP 34.14.200.220, whitelisted on Delta India. Old VM `trading-bot-worker` (35.184.66.247) systemd service stopped + disabled; VM kept for 24h soak, to be deleted ~2026-06-22. Per-coin HMM Brain (migration 0005 + symbol-keyed model+snapshot) shipped same day: tiered training (3-state-full / 3-state-diag / 2-state-diag / skip), `_market_` fallback under MARKET_SENTINEL, per-symbol regime dict in BucketRunner→sizer, dashboard regime grid per symbol. `/params` route split into index + allocation + trading + scanner sub-pages. Retrain flipped back to Binance (richer history via symbol_mapping Delta↔Binance crosswalk). 73 unit tests green.
