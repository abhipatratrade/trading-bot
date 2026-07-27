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
- [x] Run migration 0002 against prod Postgres (Railway — done 2026-06-12;
      DB lives on Railway, not GCP; migrations now auto-apply on deploy)
- [x] Redeploy bot to GCP VM (2026-06-12; Mumbai VM since 2026-06-15)
- [~] **Run on testnet ≥ 14 days unattended on the new structure** —
      ongoing; note the Phase 1c safety changes (exits, breakers, stops,
      heartbeat) landed 2026-07-06/07, so the user may want the soak
      clock to count from 2026-07-07
- [x] Train initial HMM on BTC 1D and flip `regime.enabled: true` —
      done 2026-06-14/15 (per-coin models + weekly VM retrain,
      Decision 020); `regime.yaml` has `enabled: true`
- [!] Update `allocator.yaml` μ/σ values from backtester output + new
      `backtest_ref` — BLOCKED on the backtester (separate project,
      built by the user); required before go-live
- [ ] Go live with ₹50,000 capital (user decision, after soak + μ/σ)

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
- [x] **Dedup window scaled to strategy TF** — shipped 2026-07-07:
      `dedup_window_hours_for_tf` (23/24 of one strategy bar: 1d → 23h
      as before, 1h → ~57 min, 5m → ~4.8 min); BucketRunner passes it
      per strategy row into `size_positions`. Unblocks Phase 2 swing.
- [x] **Contract sizes live + FX fixed** — contract sizes shipped
      2026-07-07: sizer uses live `contract_value` from Delta
      `/v2/products` (per-symbol override; YAML table is the fallback
      when the venue doesn't know the symbol). FX: live feed was built,
      then **superseded same day by user decision** — 1 USD = 85 INR
      FIXED for Delta India; `fx_inr_per_usd: 85.0` in each bucket's
      `allocator.yaml` is the single source (frankfurter fetch removed;
      see Decision 024 note).
- [x] **Delta client hardening** — shipped 2026-07-07: central `_request`
      with bounded safety-aware retries — GETs retry transport/5xx with
      exponential backoff; 429 retried for ALL verbs (rate-limited =
      never processed) honoring Retry-After (cap 10s); expired-signature
      errors resync a clock offset from the response Date header and
      retry; order POSTs never transport-retry (OrderManager's
      client_order_id recovery owns that). Product catalogue refreshes
      every 6h (stale kept + 10-min retry on failure).
- [x] **Regime brain cache for all TFs** — shipped 2026-07-07:
      `_window_start` generalized to any `<N><unit>` TF (15m/30m/4h/1w…);
      4h/15m buckets previously never hit the cache → per-tick HMM +
      snapshot writes. Cache now bounded at one entry per
      (bucket, symbol) instead of growing per bar.
- [x] **Kill-switch semantics refinement** — shipped 2026-07-07
      (Decision 024, user chose option c): kill switch blocks
      risk-increasing actions only. Strategy exits run while killed
      (reduce-only, `allow_when_killed`); breakers watched while killed
      (trip on a killed account with positions still flattens; halted +
      flat accounts watched silently); breaker alerts dedup-capped with
      a recovery ping.
- [x] **CSRF token on kill-switch toggle** — shipped 2026-07-07:
      per-process random token in template globals, hidden field on both
      toggle forms, constant-time check → 403 on mismatch (rotates on
      restart; stale tab just refreshes).
- [x] **Config cleanup** — shipped 2026-07-07: `kite_*` settings dropped,
      `dhan_client_id` + `dhan_access_token` added (Decision 012 / Phase
      3 prep); `.env.example` updated. Stale KITE_* env vars are ignored
      (`extra="ignore"`). `symbol_mapping.kite_symbol` DB columns left
      for a Phase 3 migration.

---

## Phase 2 — Crypto Swing [priority 2]
Started 2026-07-12 (user decision: bring remaining buckets up in parallel;
gambling-crypto deferred). Reuses every shared engine; the strategy
(`ema_9_15.py`) has existed since Phase 1a.

### 2a — Configs to live spec (2026-07-12)
- [x] `regime.yaml` real config: 1h HMM, 1500 training bars (field is a
      bar count at non-1d TF — see yaml header), hourly-scaled
      `min_mean_gap`, per-coin models for the top-10 universe
- [x] `scanner.yaml` promoted from stub (top-10 by 24h vol, $5M floor) —
      values unchanged pending backtest_ref
- [x] `strategy_master.csv` verified (ema_9_15, 1h, bull|neutral gate)
- [x] Full unit suite green (276 passed)

### 2b — Provisioning + soak (sequence matters: keys BEFORE enable —
missing `DELTA_SWING_*` creds fail the whole bot at boot by design)
- [ ] USER: create Delta India sub-account for `account_ref: swing`,
      generate testnet+live API keys, add `DELTA_SWING_TESTNET_API_KEY`
      / `_SECRET` (and `_LIVE_`) env vars on the VM
- [ ] USER (or next session): initial HMM train on the VM:
      `python -m src.shared.regime.retrain_job --bucket swing-crypto`
      (works while bucket still disabled; weekly timer takes over after
      enable)
- [ ] Flip `swing-crypto.enabled: true` in buckets.yaml, push (VM
      auto-deploys), verify: reconciler sweep, stop placement
      (stop_loss_pct 5), regime served, no breaker trips
- [ ] Soak 2-3 days on testnet (user's policy, 2026-07-12)
- [!] `allocator.yaml` μ/σ + backtest_ref from the backtester — BLOCKED
      on user's backtester run; placeholders are documented as such.
      Required before live capital (Decision 006)
- [ ] Go live (user decision, after soak + μ/σ)

## Phase 3 — Stocks Long-term integration [priority 3]
*(Detailed checklist added when Phase 2 completes. Wraps existing Kite system.)*

## Phase 4 — Stocks Swing [priority 4]
*(Detailed checklist added when Phase 3 completes. Activates Portfolio Allocator.)*

### Phase 4-interim — Blasting Momentum swing bridge (Dhan sandbox)
Standalone runner in `scripts/dhan-scanner/`, OUTSIDE the deterministic bot loop
(no Postgres / kill switch / idempotent ids / audit rows). Runs the swing-indian
spec on the Dhan **sandbox** until the real Phase-3/4 Dhan adapter + bucket_runner
land, then RETIRE it. `swing-indian` stays `enabled: false` in buckets.yaml.

- [x] Strategy files: scanner.yaml (all NSE+BSE, gap≥2%/RSI≥65↑/EMA10>20/CCI≥200/floors),
      allocator.yaml (pooled μ/σ from backtest), strategy_master.csv, blasting_momentum.py
      (ST(10,3) flip / 30d exit; indicators ported from Backtesting Engine)
- [x] Interim runner: prepare/scan/manage/status, live data + sandbox orders
- [x] Token auto-refresh via TOTP (Dhan 24h cap since 2025-10-01) — pyotp, verified live
- [x] Sandbox endpoint fixed: `sandbox.dhan.co` (was dead `api-sandbox.dhan.co`), auth 200
- [x] Dry-run made state-safe (no positions.json write); pipeline validated end-to-end
- [x] Windows Task Scheduler: DhanSwing_Prepare 18:00 / _Scan 09:44 / _Manage 15:15 IST Mon-Fri
- [ ] **OPEN — reliability**: tasks are Interactive logon (only fire when logged in);
      move to always-on VM for unattended runs (spawn-task chip raised)
- [ ] **OPEN — backtest fidelity**: backtest was Nifty 500; live universe is all NSE+BSE
      (untested microcaps). Watch candidate counts / fill quality before scaling
- [ ] **OPEN — Decision 022**: strategy has NO protective stop by design; swing-indian
      has no stop_loss_pct. Needs a Decision 022 amendment before real money

### Phase 4 — Midcap-150 1h Mean Reversion (LIVE 2026-07-27)

The real Phase-4 strategy. Decision 032. backtest_ref:
`Backtesting Engine/strategies/optimized/midcap150_meanrev_1h_swing/TRADING_BOT_HANDOFF.md`.
Plan around the TRAIN grade: ~PF 2.3, ~+25%/yr on deployed margin, ~4-5% DD,
~2 trades/day across 94 names, avg 2.7-day hold — less ~4% MTF interest. The
holdout's +57% is crash-boosted: treat corrections as upside, not the base case.
At the bucket's quarter scale (5 slots x Rs 10k) expect ~a quarter of the backtest's
rupee P&L.

- [x] Decision 032 written (quarter scale, old strategy inert, resting ATR stop, MTF interest)
- [x] `src/shared/scanner/meanrev.py` — 09:15-anchored 15m→1h resample, EMA20 distance,
      FRESH-cross rule, scale guard, prior-close daily ATR14
- [x] `engine: equity_meanrev_1h` + `run_meanrev_scan` — caches per 1h bin (~190 Dhan
      calls per bin, not per 60s tick); enforces the ≤5-new-entries-per-day budget
- [x] `mean_reversion_1h.py` Strategy — entry on the freshly-closed bin, mean-touch +
      20-trading-day exits, emits the 3.5xATR stop distance
- [x] Config set: scanner.yaml (94 pinned Midcap-150 ∩ F&O) / allocator.yaml
      (μ=0.007465 σ=0.031572 from 214 trades) / master CSV / regime OFF
- [x] Blasting Momentum made inert: `_blasting_momentum.py`, no master row, configs
      moved to `scanner_blasting.yaml` / `allocator_blasting.yaml`
- [x] Strategy-supplied resting stops: `place_order(extra_payload=)` → `Trade.extra`
      → `plan_stop_protection(stop_distances=)`. Only ever TIGHTENS vs the bucket net
- [x] MTF carry interest charged on `(notional − margin)` per calendar day at 14.6%/yr
- [x] Cadence: bucket paces to its fastest tf; `tick_interval_seconds: 60` pinned
- [x] **MTF→CNC fallback size bug fixed** — it re-sent the LEVERAGED quantity as cash
      (~Rs 38k for a Rs 10k slot, on the account shared with the user's own money).
      Now capped at 1x like the MIS path (Decision 031's guard, generalised)
- [x] Parity harness vs the 214 frozen backtest trades → **208/214** on cross, dist and
      ATR stop. The 6 misses are the EMA20 warm-up guard at the data boundary
      (5 on 2024-06-04 with 96 of 100 bins; WAAREEENER = an 11-day-old IPO)
- [x] 36 new unit tests; full suite 401 green (4 pre-existing env failures); ruff clean
- [x] **LIVE**: `enabled: true`, real money, from the first qualifying 1h close
- [x] DEPLOYED + VALIDATED 2026-07-27 23:20 IST (commit 3ef3cf0, CI green): VM selfcheck
      `enabled_buckets=['swing-indian','intraday-indian'] trading_mode=live`;
      `bucket_initialised strategies=['mean_reversion_1h']`, `tick_interval_seconds=60`,
      ticking clean (`bucket_market_closed` after hours), zero errors
- [x] READ PATH VALIDATED on the VM via `scripts/meanrev_dryrun.py`: **94/94 symbols in
      55s** (~190 Dhan calls, well inside 5 req/s), ZERO fetch errors, ZERO rejects —
      no scale-guard hits, no cold EMA20, every daily ATR14 resolved. Reused the shared
      token (`dhan_token_loaded_from_cache`), so it did NOT evict the live bot's session.
      No signal on that bar, which is the normal answer (~2 entries/day across 94 names)
- [ ] **UNEXERCISED — watch the first trade**: an MTF entry, a mean-touch exit, and the
      ATR-distance resting stop have never run against the real venue
- [ ] Confirm `carry_interest` lands on the first closed round-trip (`Trade.extra`)
- [ ] Re-derive the 94-name universe after each Midcap-150 rebalance
- [ ] Expect return BELOW the backtest: 5 concurrent slots, not 20

## Phase 4b — Stocks Intraday (intraday-indian) [inserted 2026-07-21]

NIFTY-100 gap-down reversal — Decision 029. backtest_ref:
`Backtesting Engine/strategies/optimized/nifty100_gap_reversal/TRADING_BOT_HANDOFF.md`.
Plan around the HOLDOUT grade: ~PF 1.7, ~+13%/yr on margin, ~9% DD, ~0.8 trades/week.

- [x] Decision 029 written (amends 013; capital, regime-off, wide stop, guard reformulation)
- [x] `TradingType.INTRADAY`; `intraday-indian` in buckets.yaml (₹1L, dhan, 5x, **disabled**)
- [x] Migration 0009 seeds `bucket_state` (idempotent, `enabled=false`)
- [x] Per-bucket entry window (`entry_start`/`entry_end`; 09:30–10:30 here, swing unchanged)
- [x] `src/shared/scanner/patterns.py` — TV engulfing_bull + hammer, 1:1 port
- [x] `src/shared/scanner/gap_reversal.py` + `engine: equity_intraday` (once-a-day cached cut)
- [x] `gap_down_reversal.py` Strategy — first reversal candle, 15:15 square-off
- [x] Config set: scanner/allocator (μ=0.006238 σ=0.018727 from 76 trades)/regime/master CSV
- [x] Parity harness vs the 76 frozen backtest trades → 75/76 exact (pattern + entry bar)
- [x] 19 unit tests; full suite 295 green; ruff clean
- [x] Decision 030: capital→₹50k, broad scanner set, MIS routing, shared budget, ledger P&L
- [x] Broad set: Midcap150+Smallcap100 (235 names, 15 NIFTY-100 overlaps dropped) + circuit filter
- [x] MIS product per-order (swing=MTF, intraday=INTRADAY, no CNC fallback); size fits granted margin
- [x] Shared FCFS capital budget across scanner sets; Indian P&L off the trade ledger
- [x] Live rollout staging (2026-07-22): TRADING_MODE=live on the VM (other buckets paused), dry-run systemd timer installed, .env locked to 600
- [x] Dry run VALIDATED the read path live: after a rate-limit fix, 0/99 fetch errors, margincalculator answers 3/3, liquid names get true ~5x MIS (> their MTF figure). 0 candidates = genuine no-signal morning.
- [ ] **User review, then flip `enabled: true`** ← next action, user-gated
- [ ] STILL UNEXERCISED: an actual INTRADAY order + the 15:15 square-off — needs a real qualifying signal after enabling
- [x] Per-scrip leverage: size on the scrip master's graded figure (median 4.44x N100 / 3.06x smallcap), capped by bucket ceiling
- [ ] Soak must confirm `/v2/margincalculator` works — until it does, sizing uses the scrip-master fallback
- [ ] Expect return-on-margin BELOW the backtest: it assumed 5x, real median is ~4.4x (N100) / ~3.1x (smallcap)
- [ ] Dhan sandbox soak: verify the 09:30 cut, an entry, and a 15:15 square-off end-to-end
- [ ] Confirm MIS product routing + wide stop placement on a real order
- [ ] Re-check NIFTY-100 constituents against the NSE factsheet after each rebalance

## Phase 5 — Crypto Scalp [priority 5]
*(Detailed checklist added when Phase 4 completes. Latency-tuned path.)*

## Phase 6 — Crypto Gambling [priority 6]
*(Detailed checklist added when Phase 5 completes. Memecoin pump scanner.)*

## Phase 7+ — Agentic perimeter
*(Postmortem, research, news, param tuner — advisory only, separate from core.)*

### Phase 7a — Live-session supervision (Decision 033)

Three tiers, authority DECREASING as intelligence increases. Enforcement is
capped at HALT (kill switch, reversible, exits keep running per Decision 024);
FLATTEN stays with the deterministic breakers. Keeps House Rule #1 intact.

**Tier 1 — deterministic session invariants** *(landed 2026-07-28)*
- [x] `src/safety/session_invariants.py`: six checks + `effective_holdings`
- [x] `squareoff` (HALT) — intraday products flat by 15:15 + grace. The
      square-off lives in the STRATEGY exit and the CNC fallback has no broker
      net, so nothing asserted it before this.
- [x] `stop_coverage` (HALT, 2-tick sustain) — every bot holding has a resting
      reduce-only stop; runs AFTER the sweep
- [x] `notional_ceiling` (HALT) — INR-native equity buckets only
- [x] `reject_rate` (HALT) — ≥3 rejects / 15 min
- [x] `bucket_liveness` (NOTICE) — per-bucket heartbeat row (`bucket:<id>`),
      beat after each successful pass; the process heartbeat can't see one
      wedged bucket
- [x] `foreign_positions` (NOTICE, never acted on) — makes Decision 027's
      ownership scoping visible
- [x] Wired into the 60s loop after `_sweep_stops()`; 7 new settings
- [x] 38 unit tests, pure (no DB) — checks AND the enforce/streak path
- [x] Ships OBSERVE-ONLY (`session_invariants_enforcing=false`): pages with an
      `[OBSERVE-ONLY, would have HALTED]` prefix, never touches the kill
      switch. Streaks still count, so flipping needs no warm-up.
- [ ] **UNEXERCISED LIVE**: no invariant has fired against a real session yet.
      First live square-off is the acceptance test.
- [ ] **Flip `session_invariants_enforcing=true`** once the observe-only
      alerts have agreed with reality for a few sessions ← user-gated
- [ ] Consider: data-feed staleness + Dhan token-health invariants (need hooks
      into `DhanData`), and an Indian daily-drawdown breaker off the trade
      ledger (the existing one is wallet-shaped)

**Tier 2 — intraday supervisor agent** *(designed, not built)*
- [ ] `scripts/session_snapshot.py --json` — Postgres-only session state
- [ ] Agent at 09:15 / 09:30 / 10:30 / 12:00 / 15:10 / 15:20 IST
- [ ] Authority: L1 halt on a defined whitelist, then page
- [ ] MUST NOT call the Dhan API (a second session evicts the bot's token)
- [ ] MUST NOT run on the bot VM (a dead VM would silence its own watchdog)

**Tier 3 — EOD postmortem agent** *(designed, not built)*
- [ ] 15:45 IST → Telegram digest + `docs/journal/YYYY-MM-DD.md` + `/journal`
- [ ] Per-trade slippage, signals that did NOT trade and why
      (`sizing_snapshot`), overnight assertion for `swing-indian`, rolling
      live-vs-backtest PF / win rate

## Phase 8+ — Options
*(Deferred per Goal_Setting.txt priority [10]/[11].)*

---

## Session Log

Append a one-liner per session for traceability.

- 2026-07-28 — Decision 033: session invariants (Phase 7a Tier 1) — a PROCESS watchdog beside the equity watchdog, now that two buckets trade real money. Breakers only ask "has equity fallen off a cliff?", which is right for a leveraged crypto sub-account and wrong for Indian equity, where every failure mode happens at a healthy equity. New `src/safety/session_invariants.py` runs once per 60s tick per account, AFTER the stop sweep (so "no stop" means the sweep FAILED, not that it hasn't run): `squareoff` HALT (intraday products flat by 15:15+grace — the square-off lives inside `gap_down_reversal.exits` driven by the latest BAR's timestamp, so a stale feed / tick error / rejected exit each leave it open, and the Decision 031 CNC fallback has NO broker auto-square-off behind it — a 5x intraday trade silently becomes an overnight delivery, and nothing asserted this before today); `stop_coverage` HALT w/ 2-tick sustain (one uncovered reading can race a just-placed stop); `notional_ceiling` HALT; `reject_rate` HALT (≥3/15min); `bucket_liveness` NOTICE (new per-bucket `bucket:<id>` heartbeat row — the process heartbeat keeps beating for the other buckets, so it cannot see ONE wedged bucket); `foreign_positions` NOTICE and NEVER acted on (makes Decision 027 scoping visible). THE AUTHORITY LADDER is the load-bearing decision: enforcement is capped at HALT (engage the bucket's kill switch — per Decision 024 exits + stop sweep + breakers all keep running while killed), and FLATTEN stays in `enforcement.py` reachable only by a deterministic breaker trip. That is what will keep House Rule #1 intact when the Tier 2 LLM supervisor lands: engaging a kill switch is risk-REDUCING and reversible, closing a position is a trading decision. Two subtleties: `effective_holdings` INTERSECTS the Trade ledger with exchange positions, because the ledger alone goes stale when Dhan's own MIS auto-square-off closes a position without writing our SELL row (phantom holding → square-off invariant fails forever), while positions alone can't tell the bot's rows from the user's on the shared account; and the notional ceiling is INR-equity ONLY, since Delta positions are contract-denominated and USD-priced so qty×entry_price is neither a base-unit size nor rupees. 34 new pure unit tests (checks AND the enforce/streak path), 435 green, ruff clean (the 4 reds are pre-existing local-env gaps — `apscheduler`/`jinja2` not installed here — identical on a stashed baseline). NOT YET EXERCISED LIVE — the first real 15:15 is the acceptance test. Tiers 2 (intraday supervisor agent, ~6 fixed IST wake-ups, Postgres-only, L1-halt authority) and 3 (EOD postmortem → Telegram + committed `docs/journal/` + `/journal` route) are DESIGNED in Decision 033 but NOT BUILT; both are hard-constrained to never call the Dhan API (a second session evicts the bot's token) and never to run on the bot VM (a dead VM must not silence its own watchdog).

- 2026-07-23 (cont.) — LIVE-ARMED intraday-indian, hit a token bug, fixed it, re-paused for validation. (1) The shared-account scoping fix WORKED live: on boot the reconciler logged external_position_ignored for the user's 2 NIFTY options and touched nothing (0 stops, 0 adopted, 0 orders). (2) But ~15 min in, get_positions (→ reconciler + stop sweep + breaker) failed every tick with DhanAPIError [DH-906] Invalid Token. Root cause: Dhan tokens are single-session — a peer process minting invalidates every other process's token SERVER-SIDE while it's still valid by its own JWT timestamp; the client only retried on HTTP 401 (DH-906 is HTTP 400 in the envelope) and _refresh_locked MINTED before checking the shared cache, so invalidated processes minted COMPETING tokens = N-process thrash. My own VM diagnostic scripts (each building a Dhan client) were the peer minters that triggered it. Halted the bot (systemctl stop) — no bot positions, clean. (3) FIX 9fd5f87: client _request retries once on DH-906/'Invalid Token' envelope (_is_invalid_token), and the token manager adopts a PEER's cached token BEFORE minting + records the rejected token so it never re-adopts it — breaking the thrash. 9 new token tests, 363 green. Validated live: self-heal via mint works, a second client adopts the cached token (no thrash), user positions untouched, cache restored. (4) Re-PAUSED the bucket (enabled:false, e091c06) — chose to validate the token path before real money rides on it again, NOT re-arm under the entry-window clock. KNOWN BOUNDED LIMITATION: an EXTERNALLY invalidated token (e.g. the user logging into Dhan mobile — single-session) can't recover until Dhan's 2-min mint cooldown clears; self-recovers after. OPERATIONAL: avoid logging into Dhan while the bot runs. Order path STILL unexercised. Re-arm is a deliberate user action.

- 2026-07-23 — SHARED-ACCOUNT SCOPING FIX (the 2026-07-22 hazard). New src/order_manager/ownership.py: net_owned() defines bot-owned = the bot's own net-long trades (buys minus executed sells, windowed). On a shared account (shared_account flag, True only for the Dhan account, wired in run_bot) three paths now ignore anything not bot-owned: (1) reconciler orphan-import skips exchange positions with no bot trade + caps adopted qty at the bot's own (the linchpin — also protects the exit engine, which runs off DB Position rows); (2) stop sweep plan_stop_protection skips unowned positions, never cancels the user's resting stops, sizes to bot qty; (3) breaker enforce_breakers filters positions to bot-owned before the breakers + flatten AND measures drawdown on capital+own-unrealized instead of the shared wallet (so the user's P&L can't trip the bot's breaker/flatten). Crypto unchanged (exclusive sub-accounts, Decision 019; flag defaults False). Covers BOTH intraday-indian and swing-indian. net_owned is pure + exhaustively unit-tested (models use JSONB so the suite keeps DB out of unit tests); plan_stop_protection +5 shared tests. VALIDATED live read-only against the user's REAL NIFTY option positions: bot_owned={}, 0 stops placed, 0 cancelled, 0 adopted -> PASS. 358 tests green, ruff clean. INCIDENT during dev: the first ownership test wrote 9 rows to PROD Postgres (no test-DB isolation exists — JSONB); caught immediately, deleted all 9 (verified), rewrote the test pure. Both Indian buckets remain enabled:false. Last unexercised unknown before re-arming: the ORDER path (INTRADAY placement + 15:15 square-off).

- 2026-07-22 (cont.) — Dhan token path hardened + validated live, after the dry run surfaced two more failure modes (a THIRD and FOURTH bug this session, both in live plumbing not strategy logic). (1) Transient 401 dropped scan symbols: a spurious 401 on a fresh token made _charts invalidate() discard it, and re-mint failed on Dhan's 2-min cooldown. Fixed: invalidate() keeps a last-good token; _refresh_locked() retries with a FRESH TOTP each attempt and falls back to the still-valid cached token instead of raising. (2) Cold-start mint collision: every process mints independently, so a restart or back-to-back runs cold-start into the 2-min cooldown with no token (27/99 dropped on rapid manual runs). Fixed: ported the interim tool's shared on-disk token cache into src/ — refreshable managers seed from state/dhan_token_cache.json (0600, gitignored), write it on every mint, and re-read it (peer token) before the in-memory fallback; wired via DhanData.from_settings so bot+dryrun+prepare share one 24h token. Verified live in ONE test: run 1 hit a bad TOTP window then minted on retry (attempt 2), 0/99 errors, wrote a 326B 0600 cache; run 2 (separate PID) loaded that exact token from cache with ZERO mints, 0/99 errors. Read path now fully bulletproof across restarts. 340 tests green. Still enabled:false; only the order path (INTRADAY placement + 15:15 square-off) remains unexercised.

- 2026-07-22 — Live rollout staged + a production bug caught by the dry run BEFORE any money. Did on the VM: set TRADING_MODE=live (paused longterm-crypto + swing-indian first, since the mode is process-wide and neither was asked-for on real money), installed the intraday-dryrun systemd timer (09:40 + 10:10 IST weekdays), chmod 600 the world-readable .env. Two bugs fixed en route: (1) live-mode config validator demanded DELTA_LIVE_API_KEY even with every Delta bucket disabled — now scoped to enabled brokers (would also have killed the dry run, which calls get_settings). (2) SECRET LEAK: httpx logged the Telegram bot token in plaintext to the journal on every alert — redaction was a structlog-only processor and httpx logs via stdlib; added a RedactingFilter on the stdlib handlers + fixed the Telegram regex (a stray  meant it never matched a real .../bot<token> URL and was only scrubbed by luck). USER MUST ROTATE the token; 37 historical journal lines still hold it. THE BIG ONE: the 09:40 dry run showed 95/99 gap-screen fetches failing 429 — Dhan's charts endpoint caps at 5 req/s and the scan fired ~100/s, so the live bucket would have been half-blind every morning. Fixed: 429 retry+backoff (honours Retry-After) in _charts + a 0.22s pace on the bot's client (prepare job untouched, uses bulk quote). Re-ran the dry run live: 0/99 errors, margincalculator answers 3/3, and liquid names get true ~5x MIS — HIGHER than their MTF figure, confirming the MTF fallback under-states real intraday leverage. Today's 0 candidates is now a trustworthy no-signal morning. Read path fully validated; only an actual INTRADAY order + 15:15 square-off remain unexercised (need a real signal after enabling). 331 tests green.

- 2026-07-21 (cont.) — Decision 030, five user-chosen follow-ups to 029. (1) Capital back to ₹50k: ₹50k×5x caps notional at ₹2.5L so 5×₹1L is impossible; kept per_symbol_cap 0.20 → 5 slots × ₹50k, costing ~0.02% more round-trip (~3% of edge) vs the ₹10k cliff at 0.2%/leg. (2) BROAD scanner set via Decision 026: Midcap150+Smallcap100 = 235 names after dropping 15 NIFTY-100 overlaps (they'd double-enter — dedup is per bucket+strategy+symbol and the sets run as different strategy names); `gap_down_reversal_broad` subclasses the validated strategy so logic can't diverge; explicitly NOT holdout-validated. (3) CIRCUIT FILTER — the obvious "band ≥20%" test was actively wrong: only 2 of 99 NIFTY-100 names have a 20% band because 97 are F&O with a *dynamic* band, so a width filter would have rejected the validated universe. Correct test is "F&O underlying OR hard band ≥20%", excluding 5 of 235; `fno`/`band_pct` now ride in the Dhan universe cache. (4) MIS: Dhan had NO intraday product — client was MTF-only w/ CNC fallback, so this bucket would have routed funded *delivery*. `product` is now per-ORDER (one Dhan account = one adapter); INTRADAY gets no CNC fallback (would turn 5x same-day into 1x overnight). Leverage is never predicted — new `Broker.required_margin` asks Dhan `/v2/margincalculator` and the runner fits size to the answer, degrading to 1x when unavailable. ENDPOINT UNEXERCISED — soak is its acceptance test. (5) Two holes from one root cause (components trusting a sweep-stale bucket_state mirror): size_positions ran per-strategy against full capital so two sets could each claim 100% (fixed: runner threads committed_margin, budget capped at min(wallet, capital) per Decision 027); and dashboard P&L read the SHARED Dhan wallet for every Indian bucket (the reconciler already warned "capital double-counted") — Indian buckets now use `bucket_ledger_pnl` off their own Trade rows, crypto keeps the wallet mirror. Parity still 75/76. 302 tests green, ruff clean. Still DARK.

- 2026-07-27 — **Phase 4 LIVE**: `swing-indian` re-armed on real money with Midcap-150 1h Mean Reversion (Decision 032), replacing Blasting Momentum (now inert). Holdout-validated PF 2.31 / +24.7% on margin (train fold); running at quarter scale — `capital_inr` Rs 50k x `per_symbol_cap` 0.20 = the backtest's fixed Rs 10k margin/trade, 5 concurrent slots instead of 20. New `scanner/meanrev.py` + `engine: equity_meanrev_1h`: 15m→1h resample anchored 09:15 IST (7 bins/session, the last a 15-min stub that is a real signal bar — 3 of 214 backtest trades entered at the FOLLOWING 09:15 open), EMA20 distance, FRESH -6.5% cross only, adjusted/unadjusted scale guard, prior-close daily ATR14. Scan caches per 1h bin so a 60s tick costs ~190 Dhan calls per BIN, not per minute. Three pieces of new shared plumbing: strategy-supplied RESTING stops (`extra_payload` → `Trade.extra` → `plan_stop_protection(stop_distances=)`, which can only ever tighten vs the bucket's 20% net), MTF carry interest booked on `(notional − margin)` at 14.6%/yr per calendar day (the backtest omits it, ~4% of net), and a per-bucket `tick_interval_seconds` because a 1d regime model would otherwise have paced a 1h strategy at 900s. **Found and fixed a live-money hazard on the way in**: the Dhan MTF→CNC fallback re-sent the LEVERAGED quantity as a cash order — ~Rs 38k for a Rs 10k slot, on the account shared with the user's manual trading — now capped at 1x like the MIS path. Regime gate turned OFF for this bucket (in-sample the gate cut net +30.7% → +2-9% by removing the crash-rebound trades that ARE the edge). Parity harness vs the 214 frozen trades: **208/214** on cross, dist and ATR stop; the 6 misses are all the EMA20 warm-up guard at the data boundary (Rs 13,660 of net, all winners — the port is conservative, not broken). 36 new tests, 401 green, ruff clean. UNEXERCISED: an MTF entry, a mean-touch exit and the resting ATR stop against the real venue.
- 2026-07-21 — Phase 4b BUILT (disabled): `intraday-indian`, the seventh bucket (Decision 029, amends 013), implementing the holdout-validated NIFTY-100 gap-down reversal from the Backtesting Engine handoff. New `TradingType.INTRADAY`; buckets.yaml entry at **₹1,00,000** (not ₹50k — the frozen 20% cap × 5x MIS must yield the ₹1L notional the backtest was validated at; costs ate ~99% of gross at ₹10k/trade); migration 0009 seeds bucket_state idempotently at `enabled=false`. New `scanner/patterns.py` (TV engulfing_bull + hammer, 1:1 port) and `scanner/gap_reversal.py` (`engine: equity_intraday`) — the morning cut runs ONCE per session and caches to DailyUniverse, since re-screening 99 symbols on every 60s tick would recompute a constant. `nse_session` generalised to a per-bucket entry window (09:30 here vs swing's 09:45; defaults unchanged). Regime gate deliberately OFF (fades panic — the holdout that earned +13.3% IS the Apr-25 crash); wide 15% catastrophe stop per Decision 028's logic. **Parity harness against all 76 frozen backtest trades: 75/76 exact on gap screen, pattern name AND entry bar time.** Two real bugs it caught: (1) `body_avg` is a 14-EMA that must run over the FULL multi-session series — session-scoping it reproduced only 33/76, because a gap morning's opening candles inflate the average and kill `long_body`; (2) the corporate-action guard needed reformulating for live (Dhan publishes no same-day daily bar — the 07-14 STALE-CLOSE bug), accepted cost = VEDL 2025-08-26 whose daily history is rescaled ×0.374 by the later Vedanta demerger; the scale-invariant alternative was worse (5m closes 15:25, misses the closing auction, ~1% disagreement rejected IOC/TORNTPHARM on noise). 19 new tests, 295 green, ruff clean. **Ships DARK — user flips `enabled: true` after review.**

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
- 2026-07-08 (cont.) — GCP cleanup completed per user: old `trading-bot-worker` VM (us-central1-f) DELETED with its boot disk and its static IP 35.184.66.247 RELEASED ("Address released" confirmed). Only `trading-bot-worker-mumbai` (34.14.200.220) remains. The 24h-soak grace period from the 2026-06-15 migration had long expired.
- 2026-07-08 — Three user asks shipped: (1) bucket-header rework — Regime cell replaced by Deposited / Withdrawn (Delta wallet-transaction history via new `Broker.wallet_flow_totals`, cached in bucket_state.extra by a reconciler sweep step), Cumulative Profit / Loss (realized round-trips split by sign; pairing fixed to match on bucket+symbol so breaker_flatten / stop exits pair, with out-of-window entry lookup), Total Fees — all INR at fx 85. (2) Decision 026: multiple named scanner sets per bucket (scanner_<name>.yaml + allocator_<name>.yaml, `scanner` column in strategy_master.csv, one scan per set per pass, per-set allocation). (3) GCP via browser: stopped idle us-central VM `trading-bot-worker` (running since June migration!); Mumbai VM already e2-micro (minimal) — recommended deleting old VM + releasing static IP 35.184.66.247 for the real saving. Root cause of the broken reconcile sweep was fixed earlier this day (d04e20f: Delta HMAC prehash needs '?' before query string) — wallet mirror + fills enrichment verified healthy. 202 unit tests green.
- 2026-07-14 (cont.) — Two more soak-driven fixes shipped. (1) STALE-CLOSE BUG (b2ce8dc): Dhan publishes the official daily candle only well after the close (absent at 20:50 IST on trade date; NOT a toDate-exclusivity issue — probed), so the 18:00 prepare computed prev_close + all indicators one session stale and the 09:45 gap fired vs a two-session-old close — PPAP's "+12.97% gap" was really −1.8% (backtest would have rejected; book was −3.47% / −₹1,363 unrealized at day-2 close partly because of this). Fix: `ensure_today_bar` splices today's completed session from 15m intraday; also fixed manage's session-date compare (naive UTC .date() is one session off → would double-append once Dhan publishes). Port-to-main chip raised (src/shared/scanner prepare_job runs the same 18:00 IST window). (2) PREPARE SPEEDUP 80-90min → 3min (9b3b68f): today's bar now comes from bulk /v2/marketfeed/quote (1000 ids/req, ~5 calls, official closes — slightly different from 15m-derived: shortlist 39→35) + daily-bar cache in state/history/ (200 bars/scrip, 35.8MB, 4,256 scrips) with rolling 7-11-night staggered resync, >25%-discontinuity corporate-action refetch (NSE circuit band = 20%), 7-day skiplist for 226 delisted scrips, and automatic slow-path fallback if bulk quote is down. Verified: warm full-universe sweep 3m11s, 0 fetches, idempotent shortlist. Shortlist for 07-15 scan: 35 names on true official closes.
- 2026-07-14 — Soak day 2: CLEAN — all 3 tasks exit 0. 09:44 scan: 11 candidates, 5 entries; all 5 MTF→RMS-rejected as expected, CNC fallback fired 5/5 (verify-after-POST working live): PPAP/NEWGEN/KALYANKJIL/HAPPSTMNDS TRADED, SUDARSCHEM CNC market order sat PENDING through the close (sandbox quirk; sandbox also fills everything at dummy avg ₹100). 15:15 manage: token refresh failed AGAIN at 15:15 ("Invalid TOTP" — code straddling its 30s window) → cached-token fallback saved the run (yesterday's fix earning its keep); all 5 HOLD. 18:00 prepare: 4,256 scanned / 226 errors (vs 2,933/1,655 day 1), shortlist 56. Two hardenings shipped: (1) token refresh now retries "no accessToken" responses too (fresh TOTP per attempt; rate-limit "once every 2 minutes" exempt — retrying inside the window is pointless); (2) entry reconcile in manage — positions store `order_id`, `_reconcile_today_entries` checks same-day entries against the day-scoped order book: TRADED kept, part-fills adjust qty to filledQty, 0-filled → cancel order + drop slot (dry-run previews only). Ran live: SUDARSCHEM phantom cancelled + dropped → slots 4/5 for 07-15. NOTE: sandbox wallet math runs on the dummy ₹100 fills, so sandbox P&L/utilization figures are meaningless — judge the soak on order/exit correctness only.
- 2026-07-13 — Day-one soak postmortem (interim tool): the 09:44 first entries (PPAP/KALYANKJIL/IVALUE/INDNIPPON, MTF market) were **all rejected async by sandbox RMS** ("MTF is not permitted for this Scrip") — the POST returns 2xx + orderId and the rejection only lands in the order book, so the tool logged ORDER OK and wrote 4 phantom positions; the 15:15 Manage task also died (exit 1; token refresh is transient-fragile — Dhan mints tokens max once/2min and the static fallback `DHAN_ACCESS_TOKEN` was empty). Fixes shipped to `scanner_live.py`: (1) `_post_and_verify` — every order is verified against the order book after POST; REJECTED/CANCELLED/EXPIRED = failure w/ omsErrorDescription; (2) MTF→CNC fallback now fires on async RMS rejection too (user decision: MTF stays default, CNC only when MTF unavailable); (3) manage SELLs use the position's recorded product; (4) token refresh 3× retry + last-good token cache (`state/token_cache.json`, <23h) fallback; (5) `_log` tees to `state/scanner.log` + FATAL traceback logging (Task Scheduler discards stdout). Phantom positions cleared (backup `positions_2026-07-13_phantom-rejected.json.bak`) — slots 0/5 for the 07-14 09:44 scan. VM side nominal: bucket skipped by the fail-fast probe (edge-block ticket pending), crypto unaffected. Verified: `_order_status` reads the real rejected orders; manage --dry-run green; cache fallback exercised live. Port the verify-after-POST pattern into `src/brokers/` Dhan adapter before VM cutover.
- 2026-07-12 — Phase 3+4 SHIPPED to the VM + Layer-1 deploy gate. Pushed the full swing-indian integration (M1–M8: Dhan data/broker adapters w/ TOTP refresh, equity scanner engine + daily-prepare job, NIFTYBEES regime, NSE market-hours gate, fail-soft run_bot wiring, buckets.yaml enable @ wide stop 20%, dhan-prepare systemd timer + ops/setup-dhan.sh). Soak finding: Dhan's **sandbox edge blocks datacenter IPs** (VM: sandbox.dhan.co 403 pre-auth, api.dhan.co 401 OK; no DevPortal knob) → fail-fast probe added (82a79fb): bucket skips cleanly on the VM, crypto unaffected; **live cutover unaffected** (live orders = api.dhan.co). Soak split: order path = local interim tool (first entries Mon 07-13 09:44 IST), data path = VM; full-loop VM soak awaits a Dhan support ticket for IP 34.14.200.220. Layer-1 gate (ce54986+b826838, CI GREEN): GitHub Actions (ruff+271 tests, py3.11 parity, dummy env — suite not hermetic), deploy.sh refuses non-green commits + pre-restart selfcheck entrypoint; ruff baseline repo-clean. Day-one catch: tests silently depended on local .env. Dashboard 50k = bucket_state seed; wallet-mirror pending sandbox reachability (real sandbox wallet ₹10L — Kelly sizes on it per Decision 025).
- 2026-07-10 — Phase 4-interim: took the Blasting Momentum swing-indian bridge tool live on Dhan **sandbox**. `scripts/dhan-scanner/` (from a prior session) validated + hardened: (1) Dhan capped access tokens at 24h (SEBI, 2025-10-01) — added TOTP auto-refresh (`auth.dhan.co/app/generateAccessToken` via pyotp; user enabled TOTP in web.dhan.co, creds in gitignored .env), verified minting live 24h tokens. (2) Fixed dead sandbox host `api-sandbox.dhan.co` → `sandbox.dhan.co` (DNS+auth probed, was never tested before). (3) Made `--dry-run` state-safe (no longer writes positions.json — would have blocked the next live scan). First full `prepare` sweep: 4,661 symbols → scanned 2,933, **shortlist 26**, errors 1,655 (delisted/illiquid — effective universe ~2,933). Dry-run scan validated end-to-end: 8 pass the 09:45 gap filter, top-5 sized + routed to sandbox. Scheduled via Windows Task Scheduler (DhanSwing_Prepare/Scan/Manage @ 18:00/09:44/15:15 IST Mon-Fri, pinned to Python 3.14). OPEN: Interactive-logon tasks (need always-on VM — chip raised); backtest was Nifty 500 vs live all-NSE+BSE; no protective stop vs Decision 022. First live sandbox entries: Mon 2026-07-13 09:44.
- 2026-07-07 (later) — Decision 025 (user): Kelly now sizes on live sub-account equity (available+locked mirror); capital_inr is only the P&L baseline. New `scripts/record_capital_adjustment.py` (--amount ±X on deposits/withdrawals, --rebase to zero P&L at current wallet); longterm-crypto adjustments set to −31,736.34, writing off June testnet losses (dup-order bug + liquidations, confirmed via audit trail; funding_extreme kill switch from 07-06 still engaged). Flagged: reconcile sweep appears to be failing on the VM since the `states=open,pending` change (stale wallet mirror + no fill enrichment) — spawn-task chip raised.
- 2026-07-07 (final) — Phase 1c COMPLETE. Decision 024 shipped (user chose option c): strategy exits run while killed (reduce-only, allow_when_killed) and breakers stay watched while killed (act-gated to avoid re-flatten/alert spam; per-breaker alerts dedup-capped; recovery ping). FX switched to FIXED 85 INR/USD per user (allocator.yaml source of truth; fx.py live feed removed same-day). 188 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 13 shipped: config cleanup (kite_* → dhan_* settings + .env.example). Phase 1c backlog now complete except the kill-switch-semantics item, which is explicitly waiting on a user decision. 191 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 12 shipped: CSRF token on kill-switch toggle (per-process token via Jinja globals, constant-time compare, 403 on mismatch). 191 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 11 shipped: regime brain cache generalized to any TF (`_window_start` parses `<N><m|h|d|w>`; bounded one-entry-per-(bucket,symbol) cache). 188 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 10 shipped: Delta client hardening (central `_request`: GET transport/5xx retries w/ backoff, universal 429 retry honoring Retry-After, HMAC clock-skew resync from Date header, no transport retry on POSTs; product catalogue 6h TTL with stale-keep). 183 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 9 shipped: live contract sizes + FX. `src/data_sources/fx.py` (12h-cached frankfurter.app USD/INR, sanity [50,150], last-good → YAML fallback chain); `Broker.contract_size` gained `default=` so the sizer can distinguish unknown (→ YAML) from real values; `size_positions` + `notional_inr_to_contracts` gained overrides; reconciler `fx_provider`. 176 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 8 shipped: TF-scaled dedup window (`dedup_window_hours_for_tf` in sizer; 23/24 of one strategy bar; runner passes per-strategy-row TF). 170 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 7 shipped: per-bucket tick cadence (`tick_interval_for_tf`, 1d bucket now re-scans every 15 min instead of 60s; safety paths unchanged at 60s; crashing buckets back off to their cadence) + nightly retention prune on Railway scheduler (`src/core/retention.py`; snapshots 60d, audit_log 180d). 165 unit tests green.
- 2026-07-07 (cont.) — Phase 1c item 6 shipped: heartbeat/dead-man's switch. `heartbeat` table (migration 0008), `src/core/heartbeat.py` (beat/last_beat/pure staleness), bot beats after each completed tick, Railway scheduler `heartbeat_watch` job (2 min interval) pages on stale (>600s, HEARTBEAT_STALE_SECONDS) with dedup + recovery ping. 160 unit tests green.
- 2026-07-07 (later still) — Phase 1c item 5 shipped (Decision 023): daily-anchored drawdown breaker. `daily_equity_anchor` table (migration 0007 — applied to Railway Postgres this session), first breaker pass of each UTC day snapshots account equity (wallet + unrealized), `check_daily_drawdown` rewritten as pure anchor-vs-current math so realized losses count toward the 5% trip. `ops/deploy.sh` now auto-applies alembic migrations on pushes touching `migrations/`. 155 unit tests green (was 147).
- 2026-07-07 (later) — Phase 1c item 4 shipped (Decision 022): broker-side protective stop-losses. New `src/safety/stop_protection.py` sweep (per tick per sub-account + startup) rests a reduce-only stop-market order on every open position at `stop_loss_pct` from entry (buckets.yaml; 0.5/leverage rule). Delta client gained stop-order placement (stop_loss_order/mark_price), `states=open,pending` on get_open_orders (untriggered stops), and live tick_size; OrderManager gained stop_price + "STOP" alerts; protective stops excluded from exit-engine dedup. 147 unit tests green (was 131).
- 2026-07-07 — Phase 1c items 1-3 shipped: cumulative bucket P&L (wallet equity vs capital, with capital-adjustments offset) on /buckets cards + detail page; fills/fees ingestion (Delta /v2/fills → Trade.fees + extra: avg_fill_price/traded_notional_usd; realized P&L by exit↔entry pairing; unrealized from exchange positions) in a new reconciler `_enrich_trades_pnl` sweep step; per-trade Traded amt / P&L / P&L% columns on bucket + home trade tables. Contract sizes now read from Delta /v2/products. New pure-math module src/order_manager/pnl.py + 14 tests (131 total green).
- 2026-07-06 (later) — Phase 1c backlog section added: review leftovers + two new user asks (cumulative bucket P&L on dashboard; per-trade traded amt + P&L amt/% refreshed ≤5 min, with fills-ingestion prerequisite). `continue` resumes from Phase 1c top item. Deployed c36b038 to VM + Railway; DASHBOARD_PASSWORD set on Railway dashboard service.
- 2026-07-06 — Critical-review session → Decision 021 shipped: exit engine wired into BucketRunner (step 0: `select_exits` per strategy incl. gated ones; top5_volume exits on BEAR flip, ema_9_15 on EMA state-down), breakers enforced per tick (trip → per-bucket kill switch + reduce-only flatten via `safety/enforcement.py`), reconciler now mirrors sub-account wallet into bucket_state (available/locked × allocator fx), dashboard HTTP basic auth (DASHBOARD_USER/PASSWORD) + traceback leak removed, OrderManager transport-error recovery via client_order_id lookup (no double-fire), set_leverage failure aborts placement, EntryCandidate.side honored (shorts plumbing), regime inference+retrain drop the in-progress candle, status maps gained partial/rejected. 117 unit tests green (was 73).
- 2026-06-15 — Bot-worker VM migrated us-central1-f → asia-south1-a (Mumbai). Binance HTTP 451 geoblock cleared (no more Delta-only fallback for OHLCV). New VM `trading-bot-worker-mumbai`, static IP 34.14.200.220, whitelisted on Delta India. Old VM `trading-bot-worker` (35.184.66.247) systemd service stopped + disabled; VM kept for 24h soak, to be deleted ~2026-06-22. Per-coin HMM Brain (migration 0005 + symbol-keyed model+snapshot) shipped same day: tiered training (3-state-full / 3-state-diag / 2-state-diag / skip), `_market_` fallback under MARKET_SENTINEL, per-symbol regime dict in BucketRunner→sizer, dashboard regime grid per symbol. `/params` route split into index + allocation + trading + scanner sub-pages. Retrain flipped back to Binance (richer history via symbol_mapping Delta↔Binance crosswalk). 73 unit tests green.
