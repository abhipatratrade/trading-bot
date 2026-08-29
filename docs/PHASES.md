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
- [x] **BAR-SELECTION BUGFIX 2026-08-01** — the scanner was structurally incapable of
      opening a position for its entire first live week. bugfix_ref:
      `…/midcap150_meanrev_1h_swing/MEANREV_1H_BUGFIX_HANDOFF.md`. Root cause is one
      fact with two consequences: a scan fires at HH:16, one minute AFTER the bin
      boundary, so Dhan's 15m feed already carries the bar stamped `HH:15` — the NEXT,
      in-progress bin. (1) ENTRIES: `evaluate()` required the wanted bin to be the
      newest in the frame, so it was always off by exactly one and every symbol
      returned None on every pass. (2) EXITS: `mean_touched()` read `iloc[-1]` with no
      pinning at all, i.e. the bin still forming — the opposite of what its docstring
      promised. Both now go through one helper (`locate_bin` / `_through_bin`) that
      LOCATES the wanted bin and truncates there; `ewm(adjust=False)` is causal, so
      retained values are bit-identical. Also makes the scan immune to stray bars
      (Dhan emitted a lone Sat 2026-08-01 14:30 IST bar for many NSE names, which
      under the old rule killed the scan for every symbol)
- [x] **Third defect found here, not in the handoff**: before 10:15 `last_complete_bar_key`
      named the previous CALENDAR day, so on a Monday it asked for Sunday`#6` — a bin
      that cannot exist. That made the Friday-stub → Monday-09:15 entry unreachable
      (3 of the 214 backtest trades), and the morning pass is the ONLY chance the stub
      bin gets, since the last scan of a session runs at 15:16. Now walks back to the
      previous TRADING day (holidays included)
- [x] Observability, the reason this hid for a week (Decision 033): `evaluated` was
      incremented BEFORE the cut ran, so "94 checked, none crossed" and "94 bailed at
      the first guard" were the same log line; and `ScannerSnapshot` rows were written
      only `for sig in signals`, so a zero-signal bin left no per-symbol audit trail at
      all. Now `evaluate_with_reason` → `MeanRevOutcome{signal, reason, metrics}` with
      `data_`-prefixed reasons (mirroring the gap-reversal branch), an outcome histogram
      in the audit message + payload, an `unevaluable` warning, and one snapshot row per
      EVALUATED symbol carrying whatever the cut computed before it stopped. On the bug
      day this would have logged `{'data_bin_absent': 94}` instead of "0 of 94 evaluated"
- [x] VERIFIED: repro script (drives this repo's module against cached Dhan CSVs) now
      finds SUZLON −7.0873% on `2026-07-28#5`, matching the engine to 4dp, and gives the
      SAME answer with and without the in-progress bar present. Golden case exact:
      `atr14=1.3023`, `stop_distance=4.55805` (= 3.5×ATR), entry next bin open ₹48.10.
      Production audit log independently confirms the dead week — all 28 scans 0/0,
      including the 15:16 IST pass on the exact signal bar.
      `scripts/meanrev_1h_parity.py` **unchanged at 208/214** (same six documented
      warm-up misses). 13 new/strengthened unit tests; full suite **558 green**
- [ ] **UNEXERCISED — watch the first trade**: an MTF entry, a mean-touch exit, and the
      ATR-distance resting stop have never run against the real venue
- [x] **USER DECISION 2026-08-01 — option (a), START FLAT.** The engine's book holds
      SUZLON open from 28-Jul @ ₹48.10 (Fri close 48.05 vs EMA20 48.106, ~flat, stop
      never threatened). The bot never opened it, so starting flat needs no code and no
      intervention: it simply takes the next fresh cross. Adopting the position
      mid-flight was REJECTED — it is not what the backtest did, so it breaks entry
      parity, for no P&L; and sitting 0.12% under its EMA20 a corrected bot would
      mean-touch out of it almost immediately anyway. Live book and backtest book are
      therefore knowingly one trade apart until SUZLON closes on the engine's side
- [x] **Scanner evidence now survives the session** (2026-08-03). Reviewing the
      first post-fix journal (2026-08-03: 7 passes, all 94 names reaching the
      cross test, deepest dislocation −2.14% vs the −6.5% band — a genuine
      no-signal day, and proof the bar-selection fix works) turned up a second
      recording defect. `scanner_snapshot`/`daily_universe` were keyed
      `(date, strategy_id, symbol)` and written delete-then-insert scoped to the
      DAY. Right for a once-a-day screen; destructive here, where the cut runs
      on all 7 bins: each pass ERASED the previous one, so only 15:16's rows
      ever survived — prod held exactly 94 rows, all `bar_key 2026-08-03#5`.
      Worst hit was the 09:16 pass, the only one that reads the previous
      session's 15:15→15:30 stub (the entry the backtest takes 3 times in 214),
      which left no per-symbol trace at all. That defeats the whole point of
      writing a row per evaluated symbol. Migration 0012 adds `bar_key` to both
      tables and to their unique keys; the meanrev delete is now BIN-scoped
      (same-bin re-run after a restart still replaces cleanly). Once-a-day
      scanners stamp the ISO date — for them the bar IS the day. Volume: 94 →
      658 rows/day for swing-indian, against a 77 MB database.
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
- [x] **Flipped `session_invariants_enforcing=true`** 2026-08-01 after 4 clean
      sessions (28-31 Jul). CAVEAT recorded in config.py: those sessions held
      zero positions, so 4 of the 6 checks were vacuous and act for the first
      time whenever the bot next holds something.
- [x] **Invariants now leave a DURABLE trace** (2026-08-03). They logged and
      paged Telegram but wrote NO audit row, and there was no invariant member
      in `AuditEventType` at all — so a violation reached Postgres only by
      escalating to HALT (which writes `KILL_SWITCH_FLIPPED` from
      `kill_switch.engage`). Since the EOD journal is built entirely from audit
      rows, every violation that cleared before halting anything was reported
      as "nothing tripped" — while `eod.py`'s own docstring claimed the report
      answered "did anything trip — invariants, breakers, kill switch,
      rejects?". New `AuditEventType.INVARIANT_VIOLATED` + `audit_violation()`,
      written on the SAME condition as the page (first sighting, then only on
      content change) so the audit log does not reproduce the 2026-07-28
      Telegram flood, and best-effort so a Postgres hiccup can never break the
      safety loop it exists to record. Added to `_REPORTABLE_EVENTS`.
- [ ] Consider: data-feed staleness + Dhan token-health invariants (need hooks
      into `DhanData`), and an Indian daily-drawdown breaker off the trade
      ledger (the existing one is wallet-shaped)
- [x] **`audit_log` is archived, and never deleted ahead of its archive**
      (2026-08-03). The 180-day prune was hard-deleting the forensic record
      (House Rule #8) with NO copy behind it — `core/export.py` covered `trade`
      alone. Worse, the Drive mirror had never run ONCE, for three independent
      silent reasons: (1) no credentials were ever set, so `gdrive_enabled` was
      False and the upload returned early at INFO; (2) the three `google-*`
      packages were in pyproject.toml but NOT requirements.txt, which is what
      `ops/deploy.sh` installs on the VM, so the import would have failed
      anyway; (3) the code only did SERVICE-ACCOUNT auth, which cannot work on
      a personal @gmail.com — a service account has no Drive storage quota, so
      a file it creates in a shared My Drive folder is rejected with
      `storageQuotaExceeded`, and only a Workspace Shared Drive can hold
      service-account-owned files. Now: `export_audit_log`, OAuth user-credential
      support (preferred; files owned by the user), the packages in
      requirements.txt, uploads that REPLACE rather than duplicate, and
      LOCAL ONLY promoted from footnote to alert. THE GUARD: retention's audit
      cutoff is the EARLIER of the 180-day age and an archive watermark, and
      the watermark only advances CONTIGUOUSLY — one good night cannot bless
      three months of backlog. Nothing archived ⇒ nothing deleted. Setup is the
      user's (credentials); `scripts/gdrive_authorize.py` mints the token and
      `scripts/archive_backfill.py --check/--dry-run` closes the history.
- [ ] **USER ACTION: configure Drive** (runbook → "Google Drive archive"), then
      run `scripts/archive_backfill.py`. Until then the audit prune is BLOCKED
      by design and `audit_log` grows unbounded — safe, but not free forever.
- [ ] Consider: the other three pruned tables (`scanner_snapshot`,
      `sizing_snapshot`, `regime_snapshot`, 60-day) are still deleted with no
      archive. Lower stakes than the audit log, same shape of problem.

**Tier 2 — intraday supervisor agent** *(designed, not built)*
- [ ] `scripts/session_snapshot.py --json` — Postgres-only session state
- [ ] Agent at 09:15 / 09:30 / 10:30 / 12:00 / 15:10 / 15:20 IST
- [ ] Authority: L1 halt on a defined whitelist, then page
- [ ] MUST NOT call the Dhan API (a second session evicts the bot's token)
- [ ] MUST NOT run on the bot VM (a dead VM would silence its own watchdog)

**Tier 3 — EOD postmortem** *(landed 2026-07-28)*
- [x] `src/reporting/eod.py` — pure builders + renderers, Postgres-only
- [x] Scheduler job 10:15 UTC = 15:45 IST, weekdays, skips NSE holidays
- [x] Telegram digest (phone-readable, truncates long event lists)
- [x] `session_report` table (migration 0011), one row per date, UPSERT
- [x] `/journal` dashboard route + nav link; local markdown→HTML subset
      renderer that escapes before it formats
- [x] `scripts/eod_report.py` (build by hand / backfill) and
      `scripts/export_journal.py` (materialise into `docs/journal/*.md`;
      `--commit` is opt-in and cannot restart the bot — `docs/` is outside
      `RESTART_PATHS`)
- [x] Signals seen but NOT taken, grouped by reason — first thing to ever read
      `sizing_snapshot` back
- [x] Carried-overnight section; quiet days say so instead of rendering an
      empty skeleton
- [x] 24 unit tests, pure (no DB)
- [ ] **UNVERIFIED**: migration 0011 not yet applied; no report has been built
      against real data. First run is 15:45 IST on the next trading day.
- [x] **Signal price at decision time** — `signal_price` (strategy hint) +
      `decision_price` (runner mark) on `Trade.extra`; no migration needed
- [x] Per-trade slippage, split into decision-lag vs execution
      (`src/reporting/slippage.py`), cost-positive on both sides
- [x] Rolling live-vs-backtest PF / win rate / mean return, with
      `backtest_baseline` in each `allocator.yaml` (sizer never reads it)
- [x] "Too early to read" banner below 20 closed round-trips; undefined PF
      reported as undefined, never as infinity
- [x] `win_rate` filled for both buckets 2026-08-01 from the backtest_ref JSONs
- [x] swing-indian's PF 2.31 verified: it is the TRAIN fold (82 trades),
      reproduced at 2.313. Both baselines now come from ONE fold each — they
      previously mixed folds, which benchmarked against a composite run that
      never existed.
- [ ] Exits carry no `signal_price` (`select_exits` returns bare symbols) —
      only execution slippage is measurable on the exit leg

## Phase 9 — Indian F&O: futures-indian + options-indian [Decision 036, started 2026-08-28]

Supersedes the "Phase 8+ — Options" placeholder above. Capital ₹5L per bucket,
NSE index (5) + NSE stock F&O (228), naked short premium in scope.

Phases are a dependency chain, not a menu — each gate must hold before the next
starts. **Nothing places an order before Phase D is complete.**

### A — Instrument foundations ✅ 2026-08-28
Gate: contract lookup returns correct lot / tick / expiry, re-verifiable on demand.

- [x] `TradingType` += FUTURES, OPTIONS; `futures-indian` / `options-indian`
      parse under the existing `<type>-<market>` scheme
- [x] `src/data_sources/dhan_fno.py` — `DerivativeContract` + `FnoRegistry`,
      minting a collision-free symbol from `(underlying, expiry, strike, type)`
      because Dhan's own `SYMBOL_NAME` collides across weeklies
- [x] Chunked scrip-master parse (peak bounded by chunk, not by the 74k-row
      segment) — the 2026-08-21 OOM makes this non-negotiable
- [x] Cache refreshes every 12h, not 30 days — expiries roll; scoped cache
      filename per (underlyings, expiry-window, exchange) so a scoped
      catalogue can never truncate the full one; gitignored
- [x] `NSE_FNO` resolution via `DhanData.resolve` fallback — one
      `resolve_symbol` callable serves cash equity and derivatives
- [x] `ContractSpec` on the broker contract; `DhanClient` takes a
      `contract_spec` lookup, injected like `resolve_symbol`
- [x] Per-contract tick snapping — **fixed a live latent bug**: the hardcoded
      ₹0.05 grid is wrong for 368 NSE contracts including NIFTY futures (₹0.10)
      and BANKNIFTY futures (₹0.20)
- [x] `contract_size()` returns the LOT for derivatives, 1 for cash equity
- [x] Freeze-quantity guard — refuses, never clamps
- [x] `scripts/fno_registry_audit.py` — re-measures every scrip-master claim
      against a fresh download, exits non-zero on drift. **AUDIT PASSED
      2026-08-28**: 74,322 NSE rows, 462 ambiguous names, key tuple unique,
      sentinels intact, 10,717 live index contracts parsed
- [x] 31 registry tests + 7 broker contract-spec tests; 838 green, ruff clean

### B — The spot → derivative bridge ✅ 2026-08-29
Gate: a spot signal resolves to one contract, deterministically and reproducibly.
**Cleared** — the audit now drives the selector off the real catalogue and
checks determinism plus a mint → lookup → `underlying_of` round trip.

- [x] `src/shared/contracts.py` — the symbol grammar in ONE place, so the
      registry that mints, the sizer that dedups, the reconciler that matches
      and the backtester that replays cannot drift apart. Dependency-free
- [x] `underlying_of` handles the hyphenated cash ticker `NAM-INDIA`, which a
      naive `split("-")[0]` turns into `NAM` — a live name in swing-indian
- [x] `ContractSelector` + `contract_selection:` schema and loader — ATM /
      OTM% / ITM% / OTM-steps, nearest / weekly / monthly expiry, min+max DTE.
      No new API call: everything resolves off the registry plus a spot price
- [x] `delta` strike rule REFUSED at config load rather than silently
      downgraded to ATM — nothing here fetches greeks, and a 0.30-delta
      strangle sized as ATM is a different trade
- [x] Weekly rule falls back to monthly where no weeklies list — NSE now
      lists them for NIFTY only, so without this a weekly-configured strategy
      would silently trade nothing on 232 of 233 underlyings
- [x] Deterministic tie-breaks: nearest strike ties to the LOWER strike, chain
      re-sorted rather than trusted. An off-ladder OTM-steps request is a MISS,
      never a clamp to the last listed strike
- [x] **Dedup keys on the UNDERLYING** — `dedup_keys()` in the sizer, live for
      every bucket. Identity for cash and crypto, collapsing for F&O
- [x] `contract_hint()` builds the `extra` JSONB payload (contract, underlying,
      expiry, strike, leg, lot) — rides the existing hint path, no migration
- [x] `Bucket.contracts_yaml_path_for()` — optional, Decision 026 named-set
      shaped; absence means "trade the symbol the scanner produced"
- [x] 55 new tests; 893 green, ruff clean

**Deferred to C, deliberately:** threading the execution symbol through the
runner (two price fetches per candidate — spot for the strike, premium for the
size) and onto the order. It is inseparable from lot quantisation, and a runner
that selected a contract but still sized in shares would be worse than one that
does neither.

### C — Lots, margin, cost — BUILT 2026-08-29, gate NOT yet cleared
Gate: margin preflight answers correctly against a live account. **Still
unexercised** — no Dhan account has ever answered `/v2/margincalculator`, so
this stays open until the first sandbox soak. Everything else in the phase is
built and green.

- [x] `quantize_to_lots()` — floors onto the venue's lot grid and **never
      rounds up**: one NIFTY lot is ~₹15.8L of notional against a ₹5L bucket,
      so rounding up would place an order 3× the size the allocator approved.
      Applied AFTER `_fit_to_margin`, because a margin-scaled quantity lands
      off the grid nearly every time
- [x] Sizer counts LOTS for a derivative (contract_size = lot), so the existing
      `size < 1` guard becomes "less than one lot" with no new branch
- [x] `required_margin()` MANDATORY for a derivative — no answer means no
      order. Cash equity keeps its 1× fallback; F&O has no 1× at all, because
      SPAN margin is the exchange's risk model, not a leverage multiple
- [x] The execution symbol now flows end to end (deferred from Phase B):
      `ExecutionPlan` carries contract, premium and lot keyed by UNDERLYING, so
      the scanner, regime and dedup keep seeing the underlying while the order
      goes to the contract. Pass-through for every non-F&O bucket
- [x] `Trade.extra["product"]` now recorded — its absence made cost
      attribution a guess exactly where the Decision 031 CNC fallback fires
- [x] `fee_rates.yaml` — every line carries `source` + `verified_on`, and
      `estimate_charges` REFUSES to run against an unsigned card rather than
      returning a zero (a silent zero reads as "this trade is free")
- [x] `scripts/fee_card_reconcile.py` — replays the card against REAL billed
      charges already in the ledger, so sign-off rests on evidence
- [ ] **AWAITING USER SIGN-OFF** — and the reconciliation found something.
      Four of six lines reconcile to ~0% (brokerage, exchange txn, SEBI, GST).
      STT does NOT: two buy legs were billed ₹12 on ~₹50k (~0.024%), matching
      neither the intraday rate (0.025% sell-only) nor delivery (0.1% both
      sides), while a clean same-day round trip was billed ₹0 exactly as
      predicted. Unexplained from the sources; not invented. Details in the
      card's header
- [ ] Automated drift alert in the reconciler — DEFERRED on purpose. Wiring an
      alert that is both inert (unsigned card) and unvalidated (STT unexplained)
      would be worse than none. It lands with sign-off

**Known gap:** a contract-selection miss is logged, not filed as a sizing
snapshot, so it will not appear in the EOD report's "signals seen but not
taken". `SizingDecision` is a Postgres enum and a new value needs a migration;
folded into Phase E with the buckets and their reporting.

### D — Risk: short premium and expiry
Gate: every item here holds before any order path opens.

- [ ] Dual stop, whichever fires first — exchange-resident premium stop plus a
      bot-side underlying-level exit
- [ ] **BLOCKER: mandatory pre-expiry square-off.** Stock derivatives are
      physically settled; an ITM contract carried past expiry delivers shares
      at full contract value (~₹6.7L median vs a ₹5L bucket). Enforced as a
      session invariant, not left to a strategy
- [ ] Ownership scoping under derivatives — four buckets now share one Dhan
      account; the reconciler must not adopt or square off the user's own F&O
- [ ] New session invariants: stop coverage on every derivative, nothing held
      inside the expiry window, margin utilisation under a per-bucket cap
- [ ] Position groups — multi-leg structures open/close/stop as one unit, so a
      spread's short leg is never mistaken for a naked write

### E — Buckets, strategies, dashboard, docs
Gate: **the user's backtest handoff.** Both buckets ship `enabled: false` until then.

- [ ] `buckets.yaml` blocks at ₹5,00,000 each, `enabled: false`
- [ ] Per-bucket scanner/regime/allocator/strategy_master from the handoff
- [ ] Migration seeding `bucket_state` for both, idempotent, disabled
- [ ] Dashboard positions table gains expiry / strike / type / lots columns and
      a margin-utilisation figure
- [ ] Amend CLAUDE.md's "Options: deferred until all futures/spot phases live"


---

## Session Log

Append a one-liner per session for traceability.

- 2026-08-29 (cont.) — **Decision 036 Phase C**: lots, margin, cost. Still nothing trades. (1) LOT QUANTISATION never rounds UP — one NIFTY lot is ~Rs 15.8L of notional against a Rs 5L bucket, so "round up to the minimum" would place an order 3x the size the allocator approved; below one lot the answer is zero and the runner skips. Applied AFTER _fit_to_margin because a margin-scaled quantity lands off the grid nearly every time. The sizer counts LOTS (contract_size = lot), so the pre-existing `size < 1` guard becomes "less than one lot" for free. (2) MARGIN PREFLIGHT IS NOW MANDATORY FOR A DERIVATIVE: no answer means no order. Cash equity keeps its 1x fallback because margin there IS a leverage multiple of notional; F&O has no 1x at all — SPAN is the exchange's risk model against the underlying's notional, and sizing off a leverage guess would put in an order the venue prices at multiples of the budget, with no bounded loss behind the mistake on a short option. (3) The execution symbol now flows END TO END (deferred from B): `ExecutionPlan` keys contract, premium and lot by UNDERLYING, so scanner/regime/dedup keep seeing the underlying while the order goes to the contract; pass-through for every non-F&O bucket, so there is no second code path. (4) FEE RATE CARD, and this is the part that earned its keep. Every line carries `source` + `verified_on`; `estimate_charges` REFUSES an unsigned card rather than returning zero (a silent zero reads downstream as "this trade is free"). Rates fetched from Dhan/Zerodha/Angel One and CROSS-CHECKED: F&O STT moved on 1 April 2026 (futures 0.02->0.05%, options 0.10->0.15% on premium, sell side), and the two sources DISAGREE on futures stamp duty (0.002% vs 0.0001%) — recorded in the line's note rather than smoothed over. THEN reconciled the card against real billed Dhan charges: 4 of 6 lines land at ~0% drift, but STT does NOT. Two buy legs were billed Rs 12 on ~Rs 50k (~0.024%), matching neither intraday (0.025% sell-only) nor delivery (0.1% both sides), while the one clean same-day round trip was billed Rs 0 exactly as predicted. I could not explain it from the sources and did not invent an explanation — the card ships UNSIGNED with the finding in its header. Also found: the entire SELL side is unvalidated (all three billed orders were buys) and stamp-duty actuals look rounded to whole rupees. (5) `Trade.extra["product"]` is now recorded, because its absence made cost attribution a guess exactly where the Decision 031 CNC fallback fires. 19 new tests, 912 green, ruff clean. GATE NOT CLEARED: `required_margin()` still has never run against a live account.

- 2026-08-29 — **Decision 036 Phase B**: the spot→derivative seam. Still nothing trades. Three pieces. (1) `src/shared/contracts.py` — the contract symbol grammar in ONE dependency-free place, because a grammar duplicated across the registry, the sizer, the reconciler and the backtester is one that will disagree in three of them. THE TEST THAT EARNS ITS KEEP: `underlying_of("NAM-INDIA")` must return `NAM-INDIA`, and the obvious implementation — `symbol.split("-")[0]` — returns `NAM`, silently breaking the dedup gate for a name swing-indian trades with real money today. The regex anchors on the 8-digit expiry and matches the underlying greedily instead. (2) `ContractSelector` + a `contracts.yaml` block: ATM / OTM% / ITM% / OTM-steps strikes, nearest / weekly / monthly expiries, min+max DTE, all resolved off the registry and a spot price with NO new API call. The `delta` rule is REFUSED at config load rather than quietly downgraded to ATM — nothing here fetches greeks, and a 0.30-delta strangle sized as ATM is a different trade with a different loss profile. Weekly falls back to monthly where none list, which matters because NSE now lists weeklies for NIFTY ONLY: without the fallback a weekly-configured strategy trades nothing on 232 of 233 underlyings, silently. Tie-breaks are total (nearest strike ties LOW, chain re-sorted rather than trusted) and an off-ladder OTM-steps request is a MISS, never a clamp to the last listed strike. (3) DEDUP NOW KEYS ON THE UNDERLYING, live for every bucket — identity for cash and crypto, collapsing for F&O. Without it the ledger holds contract symbols while the scanner offers underlyings, the gate never matches, and a strategy already short one NIFTY strike opens a second: two strikes on one index are ONE bet with two spellings, and the per_symbol_cap would believe it had capped exposure it had doubled. Extracted as `dedup_keys()` so it is testable — nothing in the suite calls `size_positions`, by design, and an untested inline set-build is exactly where this regresses. The audit script now drives the selector off the REAL catalogue and checks determinism plus a mint→lookup→underlying round trip; **AUDIT PASSED 2026-08-29** across all 5 index underlyings and 3 rule sets. 55 new tests, 893 green, ruff clean. DEFERRED TO C on purpose: threading the execution symbol through the runner and onto the order — inseparable from lot quantisation, and a runner that picks a contract but sizes in shares is worse than one that does neither.

- 2026-08-28 — **Decision 036 Phase A**: instrument foundations for the two Indian F&O buckets (`futures-indian`, `options-indian`, ₹5L each, NSE index + stock F&O, naked short premium in scope). Nothing trades — neither bucket exists in buckets.yaml yet. THE FINDING THAT SHAPED THE MODULE: Dhan's `SYMBOL_NAME` is NOT unique — it carries only the expiry MONTH, so `NIFTY-Sep2026-23150-CE` names FIVE different weeklies (462 ambiguous names over 2,236 NSE contracts), and keying on it would silently trade the wrong expiry and book cleanly while doing it. New `src/data_sources/dhan_fno.py` mints its own symbol from `(underlying, expiry, strike, option_type)`, verified unique across all 74,322 NSE rows, and LOGS a collision rather than shadowing one. Parse is CHUNKED, not a wider version of the equity full-frame read — the D segment is 74k of 197k rows and would have added ~60MB on the 958MB VM that OOM'd on 2026-08-21. Cache refreshes every 12h (expiries roll) with a scope-digest filename so a scoped catalogue can't truncate the full one. LATENT BUG FIXED: `_snap_tick` hardcoded ₹0.05, but tick is per-contract and index futures don't use it — NIFTY/FINNIFTY tick ₹0.10, BANKNIFTY/NIFTYNXT50 ₹0.20, 368 NSE contracts coarser than ₹0.05 (up to ₹5.00) — so this would have had orders REFUSED off-tick on the most liquid contracts in the market. Harmless in cash equity, which is why it survived. New `ContractSpec` on the broker contract carries lot/tick/freeze, injected like `resolve_symbol`, so the adapter still knows nothing about the scrip master and every cash-equity caller is byte-identical. Freeze-quantity guard REFUSES rather than clamps (a clamped entry opens a position the stop wasn't computed for). Also corrected my own earlier count: 1,654 genuine fractional strikes, not 2,319 — the higher figure wrongly counted the 665 futures `-0.01` sentinels. New `scripts/fno_registry_audit.py` re-measures every scrip-master claim against a fresh download and exits non-zero on drift; **AUDIT PASSED 2026-08-28** end to end, 10,717 live index contracts parsed. 38 new tests, 838 green, ruff clean on src/tests/. ONE INFERRED NUMBER, flagged: the per-contract tick VALUE is read, but the paise→rupee DIVISOR is calibrated off NSE cash equity's known ₹0.05 — wrong by exactly 100× if that calibration is wrong, which the first live order would reject loudly. STILL UNVERIFIED: `required_margin()` never run live, Super Order on F&O, the carry-forward product string, Dhan's F&O `tradingSymbol` format (mitigated by the `by_security_id` reverse index).

- 2026-08-01 — Closed out Decision 033's rollout: invariants ENFORCING, baselines corrected, stale rows cleaned. (1) ALERT SPAM BUG I shipped 2026-07-28: `enforce_session_invariants` called `send_alert_dedup` every tick per violation, and that helper's window RE-ARMS HOURLY — right for a transient error that recurs, wrong for a permanently-true condition. `foreign_positions` is violated continuously while the user holds anything on the shared Dhan account, so it paged 3×/hour ≈ 72 msgs/day about positions the bot correctly ignores, and worse, buried the observe-only alerts the period existed to collect. Now keyed on a digest of what would be SAID: pages on appearance, again on content change, silent otherwise until it clears. Two subtleties the naive version got wrong — `would_halt` is IN the digest (so escalation from "seen once" to "would have HALTED" still pages), and a check whose detail moves on its own overrides via new `alert_signature` (bucket_liveness carries an age that grows every tick; without it a stalled bucket pages forever). (2) ENFORCING ON: `session_invariants_enforcing` defaults true after 4 clean sessions (28–31 Jul, user confirmed no observe-only alerts). Config comment records the evidence HONESTLY: those sessions carried ZERO positions and ZERO orders, so squareoff/stop_coverage/notional_ceiling/reject_rate were all vacuous — only bucket_liveness was genuinely exercised and foreign_positions fired as designed. FOUR OF SIX CHECKS WILL ACT FOR THE FIRST TIME on the first day the bot holds something; what bounds that is the authority ceiling (HALT only, exits+stops keep running, reversible), not the observe period. (3) BASELINES were MIXING FOLDS — my error. intraday had PF from holdout but trades/mean from the full run; swing had PF 2.31 from an unidentified fold paired with the full run's 214 trades. Read the backtest_ref JSONs (they live at `Backtesting Engine/results/scanners/`, NOT the `strategies/optimized/...` path the YAML comments claimed) and recomputed each fold end-to-end. THE 2.31 MYSTERY IS SOLVED: TRADING_BOT_HANDOFF.md §3 has a three-fold table and 2.31 is the TRAIN fold (2025-07→2026-07, calm, 82 trades) — reproduced exactly at PF 2.313 / win 73.17% / mean unlevered 0.015094. buckets.yaml's "holdout-validated … (train fold)" is loose wording, not a second number. Train is also the RIGHT baseline and deliberately not the highest: holdout is PF 3.04 because mean-reversion FEASTS on the Apr-2025 crash, so benchmarking against calm tape treats corrections as upside rather than base case. intraday likewise pinned to its holdout (PF 1.684 / win 51.43% / 35 trades) per scanner.yaml's "plan around the holdout grade". Both `win_rate` fields now filled from the same fold as everything else. (4) PROD DB WRITE (user-approved): rows 244/245 — the user's Jul-2026 NIFTY options orphan-imported 2026-07-22 before the scoping fix, contracts since expired — flattened with a guarded UPDATE (`and bucket_id is null and side <> 'FLAT'`, asserted rowcount==2); 0 non-FLAT remaining. Dashboard home route + HTMX partial now scope to `bucket_id IS NOT NULL` so an orphan row can never again render as the bot's. 528 green, ruff clean.

- 2026-07-28 (cont. 3) — SIGNAL PRICE AT DECISION TIME, which unlocks both deferred Tier 3 sections. The reason it mattered enough to jump the queue: "what the strategy saw" is NOT something the exchange knows, so it is unrecoverable unless recorded at the moment of decision — every day it went unrecorded was live evidence permanently lost, with two buckets already on real money. No migration needed: the `hint` → `_entry_extra` → `Trade.extra` JSONB path already carried `stop_distance`, so `signal_price` (from the strategy) and `decision_price` (the runner's mark at placement) just join it, alongside the reconciler's existing `avg_fill_price`. Three prices split the gap into two costs with COMPLETELY DIFFERENT FIXES: decision lag (`decision_price − signal_price`) is scan latency / tick cadence, execution (`fill_price − decision_price`) is spread + impact + order type. Reporting only the total would say "you're losing 16bps" without saying which one to go fix. Sign convention: positive is ALWAYS a cost, on both sides of the book, so entries and exits average together instead of cancelling into a comforting zero. Both live strategies now emit `signal_price` (mean_reversion_1h → `sig.close`, which `MeanRevSignal` already carried; gap_down_reversal → the pattern candle's close). Exits get `decision_price` only — `select_exits` returns bare symbols so there's no per-symbol reference bar without changing that contract for every strategy; the execution half is the actionable half anyway, and it costs one exception-safe `get_ticker` per exit (~0.22s under Dhan pacing, negligible against the 15:15→15:30 window). LIVE-VS-BACKTEST: new optional `backtest_baseline` block in each bucket's `allocator.yaml` — the file that already carries `backtest_ref` and pooled mu/sigma, so House Rule 7 holds; it is purely descriptive and THE SIZER NEVER READS IT. PF and win rate are scale-invariant, which is exactly what makes a live rupee figure comparable to a backtest figure on unlevered returns. Two deliberate honesty guards: a live PF with no losing round-trip yet is UNDEFINED not infinite (printing ∞ beside a backtest's 2.31 would read as spectacular rather than "too early"), and below 20 closed round-trips the section leads with a "too early to read" banner — both buckets went live this month, so every report for months will be under it and a 3-trade PF of 4.90 must not read as a verdict. `win_rate` baselines left BLANK for both buckets: not recorded anywhere in this repo, and a guessed baseline is worse than none — the report omits what it isn't given. New `src/reporting/slippage.py` (pure) + `profit_factor`/`win_rate` in `pnl.py`. 21 new slippage tests + 12 new report tests, 507 green, ruff clean. Smoke-tested vs prod (edge section correctly absent — no closed round-trips yet) and rendered against synthetic data to confirm the output is actually readable. TODO for the user: fill the two `win_rate` baselines, and verify swing-indian's PF 2.31 against its backtest_ref JSON (the Decision 032 comment's "(train fold)" parenthetical is ambiguous about which fold produced it).

- 2026-07-28 (cont. 2) — DEPLOYED Tiers 1+3, then smoke-testing `gather()` against PROD Postgres caught TWO bugs the unit tests had missed. Deploy verified end-to-end: migration 0011 applied ~75s after push, and the NEW per-bucket heartbeat rows (`bucket:swing-indian` / `bucket:intraday-indian`, both fresh) prove the restart took the new code AND that both live buckets are completing pipeline passes — the Tier 1 wiring is running in production, observe-only. BUG 1: `AuditLog` has NO `created_at` column — it is the single model that does not inherit `TimestampMixin` (its timestamp is `ts`), so the report's Events section would have raised UndefinedColumn at 15:45 on the first day an event was logged. The tests missed it because they duck-type the ORM to stay DB-free, so the fake was written to match the WRONG assumption and passed happily; added `test_fakes_match_the_real_orm_columns`, which asserts every attribute the fakes expose actually exists on the real mapper, and verified it fails on the original code. BUG 2 (the important one): the report claimed the USER'S OWN positions as the bot's. It read `Position` rows with no ownership scoping, so rows 244/245 — the NIFTY options orphan-imported 2026-07-22 BEFORE the scoping fix (1fe42e1) and still non-flat — were reported as "carried overnight" with ₹21,479 of notional. That is the 2026-07-22 failure mode exactly (read the ACCOUNT, call it the bot), re-told in prose. New `split_positions()` disqualifies on either of two independent grounds — no `bucket_id` (Decision 013 stamps one on every bot position), or absent from the bot's own Trade ledger — with the ledger test scoped to the shared Dhan broker only, since crypto sub-accounts are exclusively the bot's (Decision 019) and `owned` is built from Dhan trades alone. Foreign positions are NOT hidden (its own kind of lie): they get a clearly-labelled section stating they're excluded from every P&L figure, and it renders on quiet days too so bot silence never reads as an empty account. Re-smoke-tested vs prod for 2026-07-27: correctly a quiet day for the bot, with the 2 NIFTY options listed as not the bot's. 35 EOD tests (was 24), 474 green, ruff clean. SPAWNED FOLLOW-UP: rows 244/245 are stale prod data that also render on the dashboard `/` home route (which selects `side != FLAT` with no ownership scoping) as if the bot owned them — needs flattening + a route fix + possibly a reconciler change so it can't silently re-accumulate.

- 2026-07-28 (cont.) — Phase 7a Tier 3 shipped: the EOD session postmortem. New `src/reporting/eod.py` runs on the Railway scheduler at 10:15 UTC = 15:45 IST (weekdays, re-checks `is_trading_day`), AFTER the 15:15 square-off and the 15:30 close so what is still open genuinely IS carried overnight. The nightly Parquet export already archives the ledger at 06:00 IST the next morning, but that is an ARCHIVE not a REPORT — it says what was traded and nothing about whether the session behaved. This answers what you actually want at 15:45: per-bucket realized + fees; entries/exits/rejects tables; **signals seen but NOT taken grouped by reason** (straight from `sizing_snapshot`, which has held that record since Decision 026 and which nothing had ever read back — the literal answer to "why didn't it trade today?"); carried-overnight with notional; and events (breakers, kill-switch flips, regime changes, reconcile diffs) off `audit_log`. Three outputs from one source: a phone-readable Telegram digest, a `session_report` row (migration 0011 — Postgres is the store because the Railway scheduler container is EPHEMERAL and has no git credentials), and a `/journal` dashboard route. `scripts/export_journal.py` bridges Postgres → `docs/journal/*.md` for git; `--commit` is opt-in and cannot restart the bot, since ops/deploy.sh's RESTART_PATHS excludes `docs/`. `scripts/eod_report.py` builds one by hand for a backfill or re-run. The dashboard renders markdown with a ~60-line local subset renderer rather than a CommonMark dependency (the generator emits a known closed grammar), and it ESCAPES BEFORE IT FORMATS so a broker message or symbol with angle brackets can't inject markup. A quiet day says so plainly instead of rendering an empty skeleton — both live strategies wait for a specific setup and most days don't offer one, and a report that looks broken on a normal day is one you stop reading. Same hard constraint as Tier 1: Postgres ONLY, never the Dhan API (a second session evicts the bot's token). 24 new pure unit tests, 463 green, ruff clean on the enforced scope (src/ tests/; note scripts/ and migrations/ carry ~52 PRE-EXISTING UP007/UP035/UP017 findings and have never been in that scope — the new migration deliberately matches its 10 siblings' `Union[str, None]` style). NOT YET VERIFIED: migration 0011 is unapplied and no report has run against real data — first live run is 15:45 IST on the next trading day. Deliberately deferred: per-trade slippage vs signal price and rolling live-vs-backtest PF/win-rate, both of which need a signal-price record the ledger doesn't currently carry.

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

- 2026-08-03 (later) — Drive archive made real, and 0012's own backfill bug fixed. VERIFIED IN PROD after the 0012 deploy: `bar_key` on both scanner tables, `INVARIANT_VIOLATED` in the enum, and the new invariant row fired within minutes — `foreign_positions` recording the user's four open NIFTY-Aug2026 option legs as NOT the bot's (NOTICE, never acted on, exactly per Decision 027). That is the first invariant ever to leave a durable trace. BUG FOUND IN MY OWN 0012: `ADD COLUMN ... DEFAULT ''` populates every existing row on the spot, so the `WHERE bar_key IS NULL` backfill matched nothing and all 2,342 scanner_snapshot + 168 daily_universe legacy rows got `''` rather than their ISO date. Nothing lost (`date` still carries the day) but it wasted the `''` sentinel, which 0012 documents as "written inside a deploy window" — migration 0013 backfills `WHERE bar_key = ''`. The server_default STAYS: deploy.sh migrates before it restarts, so old code briefly inserts against the new schema, and without the default that raises inside a scan on a live process.
- 2026-08-03 (later still) — Drive archive VERIFIED END-TO-END against the real API: user set the OAuth vars, `scripts/archive_backfill.py` uploaded 2026-05-01..2026-08-02 and set the watermark, so the audit prune is unblocked and the mirror is proven (it had never once run before today). Env vars belong on the RAILWAY scheduler service, NOT the VM — the VM runs bot-worker; `_nightly_export` is a Railway job. I told the user the VM at first; that was wrong. Two bugs found by running it for real: (1) `--from` combined with `force=True` would have let a PARTIAL backfill set a watermark asserting everything below it was archived — the exact data-loss the guard exists to prevent, built into the tool that repairs it; now a gap-leaving run refuses to move the mark and says why. (2) cp1252 Windows consoles cannot encode the scripts' own em-dashes/arrows, so the run died with a UnicodeEncodeError that read like an archive failure; both scripts now force UTF-8 on stdout. NEW: consolidated trade ledger (`src/reporting/tax_ledger.py` + `scripts/export_trade_ledger.py`), for RECONCILIATION against the broker statement, never as a filing document. Scoping it exposed the real state of the ledger: of 6,060 `trade` rows only **44 are FILLED**, all `longterm-crypto`, 2026-05-02..07-12; **zero Dhan fills have ever happened**; `price` is NULL on fills (real price is `extra.avg_fill_price`); `fees` is populated on 12 of 44 with no brokerage/STT/stamp/GST breakdown; and the user's own manual NIFTY option legs are absent by construction (the bot rows only what it placed). Two traps the ledger must not fall into, both pinned by test: realized P&L is stamped on BOTH legs of a round-trip (`pnl_usd` on the closing SELL and back on the opening BUY via `closed_by_trade_id`), so it is attributed to the CLOSING leg only; and currency is per-broker and NEVER converted — allocator.yaml's fixed 85.0 USD/INR is a sizing convention (Decision 024), not a market rate on any trade date. Also fixed test isolation: the gdrive config tests built bare `Settings()` and inherited `.env`, so they inverted the moment real credentials existed — they now pin all five gdrive fields explicitly. 598 green, ruff clean.
- 2026-08-04 — Archive dead-man's switch. `_nightly_export` alerts on a failed upload, but only if the job RUNS: a dead Railway scheduler container stops the archive and every alarm about it in one stroke, which is the same class of blind spot the whole mirror had for months. So the watcher runs on the VM inside `run_bot`'s tick loop (`_check_archive`, hourly + once on the first tick), NOT on Railway — the exact mirror of the Railway-side heartbeat watch that exists because a dead VM must not silence its own watchdog (Decision 020/033). It is pinned by a test asserting `_check_archive` appears in run_bot and NOT in run_scheduler, because moving it later would silently reintroduce the blind spot. One symptom (a watermark that stops advancing) covers all three failure modes: scheduler down, Drive token expired, upload failing nightly. `archive_stale_days=2` — a healthy lag is 1 (the job archives YESTERDAY, seven days a week, and advances the mark even on a no-row day), so 2 tolerates one missed night. Also records that the alarm is the same signal that explains a growing audit_log, since the prune is gated on that mark. User published the OAuth consent screen, so the 7-day Testing-mode refresh-token expiry no longer applies. 612 green, ruff clean.
- 2026-08-07 — **Health check found a two-day blind outage, plus a live credential in a public repo.** Routine "is everything running fine" check. Bot uptime, heartbeats, archive watermark (lag 1, healthy), session reports and today's scans were all fine — but 2026-08-04 and 08-05 both live Indian buckets scanned **ZERO symbols for two full trading days**, and it **recurred 08-07 at 15:15**, killing the last scan of the session. ROOT CAUSE: the Dhan token went dead (401 on every data call); the self-heal tried to re-mint; Dhan refused with `"Token can be generated once every 2 minutes"` delivered as **HTTP 200 with the error in the BODY**, so `raise_for_status()` passed it; the retry is 3 attempts 2s apart (tuned for a bad 30-SECOND TOTP window, not a 2-MINUTE mint limit) so all three landed inside the same lockout and each refusal re-armed it; it then fell back to `_last_good_token`, which was timestamp-valid and dead, and looped — **3,831 mint attempts on 08-04, 3,336 on 08-05** (vs 21 and 16 on healthy days). Every symbol's fetch raised into `except: continue`, so the scan logged `0/0 crossed of 0 evaluated` and the EOD journal said "Scanners ran 11 pass(es); nothing qualified." The only durable trace was two `bucket_liveness` NOTICEs, fired incidentally because 94 symbols × ~7s of retries made one pass take 2,245s. No alert, no breaker, no HALT. Cost was zero — mean reversion produced no signals on 08-03 or 08-06 either — which is luck, not design. FOUR FIXES SHIPPED (03a7e80): (1) `auth.py` recognises the 200-with-error-body refusal and does NOT retry into it, gates mints behind a real 130s cooldown, and stops serving a server-REJECTED token forever — the last-good fallback still gets `_MAX_REJECTED_SERVES` goes for the spurious-401 case (2026-07-22) then raises. (2) `logging.py` — the same mint URL put the live Dhan **PIN** in journald in plaintext on every attempt; every redaction rule was SHAPE-based (hex ≥32, base64 ≥40, `digits:35-chars`) and a 6-digit PIN matches none, nor could any value-shaped rule catch it without also redacting prices and security ids, so sensitive query params are now redacted by **NAME**. (3) NEW `scan_coverage` invariant — the first that watches PERCEPTION rather than positions; attempting 0 of a non-empty configured universe, or evaluating 0 of a non-empty attempted one, HALTs (a pinned git universe does not legitimately shrink to nothing); mostly-unusable data is NOTICE only; a payload without the counts reads as UNKNOWN not zero, so the crypto scanner and every legacy row cannot manufacture a halt. (4) `eod.py` carries the configured→attempted→evaluated funnel, refuses the word "quiet" unless something was evaluated, and says SCANNERS BLIND when passes ran and saw nothing. SEPARATE AND MORE SERIOUS: the **live Dhan client id + PIN were committed as test fixtures** in `tests/unit/test_dhan_config.py` since f03962d, in a **PUBLIC** GitHub repo. Scrubbed to dummies; the TOTP secret there was always the RFC-4226 example value, so it was never exposed, and PIN+client_id alone cannot mint an API token — but the PIN is the Dhan LOGIN pin. Rotation is the user's action; history rewriting does not un-publish it. 637 green, ruff clean on src/ + tests/. NOT YET PUSHED — awaiting the user's go-ahead, because pushing auto-deploys the VM and this adds a HALT-capable invariant to a live money system.
- 2026-08-07 (cont.) — **Corrected my own over-aggressive halt, and closed the gap it could not see.** User pushed back on the HALT design and was right. (1) `scan_coverage` is now NOTICE-only. A blind scanner evaluates nothing → produces no signals → enters nothing; the kill switch blocks risk-INCREASING actions (Decision 024), so halting a bucket that has already stopped increasing risk prevents nothing. `check_bucket_liveness` states this exact reasoning for its own NOTICE. And the halt had a real cost: `kill_switch.disengage()` has EXACTLY ONE caller (the dashboard button), so nothing clears a halt automatically — the six-second token blip that hit the 15:15 bin on 2026-08-07 would have halted swing-indian overnight and skipped the next morning until someone clicked. Swapping a silent two-day outage for an unattended overnight stop is not an improvement. (2) `scan_coverage` could not have caught 2026-08-07 anyway: that scan was HEALTHY (94 evaluated, BLUESTARCO found at −6.58% dislocation, Kelly approved at ₹40,000). The trade died one step later — `/v2/marketfeed/quote` 401'd on the dead token, the sizer had no price to convert rupees into shares, and it filed the miss as `skipped_other`, the same code it uses for "I decided not to". Retried 38× over an hour, aged out of its bin, no alert, no trade. ROOT DEFECT: a data FAILURE and an allocator DECISION were recorded identically — the same mistake the scanner made before Decision 033 taught it to prefix unevaluable outcomes with `data_`; the sizer never learned it. It lives in the SHARED `bucket_runner._collect_mark_prices` + `allocator.size_positions`, so **every bucket has it, crypto included**. Exits are NOT affected — `_collect_mark_prices` at bucket_runner.py:591 feeds slippage reporting only and a missing mark never blocks the reduce-only order. FIX: `_collect_mark_prices` now reports which fetches RAISED; the sizer distinguishes `PRICE_FETCH_FAILED_REASON` from `NO_MARK_PRICE_REASON`; new `signal_delivery` invariant (NOTICE) pages when a signal the strategy DID produce could not be sized because the broker fetch failed. Matched on the constant, not a substring, with a test pinning that, since a reworded literal would silently disarm the alarm. NET: no invariant added this session can halt anything — all four new/changed outcomes are NOTICE. 645 green, ruff clean. Still NOT PUSHED pending the user's go-ahead; PIN rotation is still outstanding and is the user's action.
- 2026-08-07 (final) — **The real bug: `client-id: ""` on `/v2/marketfeed/quote`.** User asked the question that broke my own explanation open: Dhan's mint cooldown is 2 minutes, so why did BLUESTARCO fail for a full hour? It didn't add up, and it wasn't the token. The mint log for that hour alternates SUCCESS / refused / SUCCESS / refused every ~95s — the bot was minting fine throughout; the refusals were just the ~95s tick beating the 120s cooldown every other pass. ROOT CAUSE: `/v2/marketfeed/quote` authenticates on **access-token AND client-id**, and `src/data_sources/dhan.py::_quote` hardcoded `"client-id": ""`. **76 of 76 calls 401'd — the endpoint had NEVER once succeeded.** Dhan says so in the body (`{"Data":{"810":"ClientId is invalid"},"status":"failed"}`) but the handler only read the status code, and 401 was assumed to mean "token expired". PROVEN against the live API (read-only, token read straight from the shared `dhan_token` Postgres row so nothing was minted or evicted): empty client-id → 401 `ClientId is invalid`; real client-id → 200 with full quote data for BLUESTARCO. WHY IT HID: the charts endpoints do NOT require client-id, so the SAME token returned 200 thousands of times a day right beside the failures — every obvious check on the token said healthy, because it was. WHAT IT COST: every mark price comes through this endpoint and no entry can be sized without one, so **swing-indian has been structurally incapable of opening a position since go-live 2026-07-27** — the actual explanation for "zero Dhan fills have ever happened", which had been recorded as an observation and never diagnosed. It also DROVE the token churn: `get_ticker` reads 401 as "token expired" → invalidate → re-mint, so a permanently-401ing endpoint re-minted on a loop (139 successful mints on 08-07 alone), and Dhan being single-session each mint invalidated the session the other endpoints were using — a strong candidate for the 08-04/05 charts-401 storm too (charts: 2,292 × 401 vs 1,864 × 200 over three days), though that link is NOT proven. BLAST RADIUS: the bug is in the Dhan adapter, and `run_bot.py:241` builds ONE `DhanData` shared by every Indian bucket — so **swing-indian, intraday-indian and longterm-indian are all affected**; no crypto bucket is (`delta_india.py` has its own `get_ticker`, proven by longterm-crypto's 44 filled trades May–Jul). intraday-indian was affected but LATENT: it found 0 gap-down candidates all week, so it never reached the sizing step — it wasn't working, it was untested. Its scanning is fine (`prepare_job` uses `get_ohlcv`/charts, no client-id needed); only sizing broke. FIX: `DhanData` takes `client_id`, `from_settings` supplies it, both the initial call and the 401-retry send it, and a missing one raises BY NAME rather than emitting a 401 the caller will misread as an expired token and answer by re-minting forever. `brokers/dhan/client.py:200` already passed it correctly — the bug was isolated to the data adapter. 648 green, ruff clean. **PUSHED 2026-08-07 (10462c0..27a41f1)** — five commits, VM auto-deploy. NOTE FOR THE NEXT SESSION: swing-indian can now place an entry for the FIRST time ever; the next signal it produces will be the first real test of the whole entry path (sizing → `_fit_to_margin` → MTF order → protective stop). Watch it. STILL OUTSTANDING: **the user must rotate the Dhan PIN** — it was committed in `tests/unit/test_dhan_config.py` from f03962d until today in a PUBLIC repo, and scrubbing it does not un-publish it.
- 2026-08-10 — **intraday-indian could never have traded either, and the fix is NOT the obvious one.** The new `scan_coverage` invariant fired on its first live morning (Telegram, 09:16 + 09:33) and surfaced a second structural blindness, this one in intraday-indian: **0 `daily_universe` rows and 0 `passed` snapshot rows, ever** — not "no gaps qualified", but never once screened successfully. TWO bugs stacked. (1) `_MIN_SESSION_BARS = 6` is bars through 09:45, but the screen reads exactly `today[0].open` (09:15) and `today[2].close` (the 09:25 bar, closing 09:30) and never touches index 3+ — so it needs THREE. It fired on the first tick after the open (~09:31), found 4 bars, and rejected all 99/230 names with `data_too_few_session_bars`. Now **4**, not 3, because Dhan's 5m response carries the in-progress candle (observed today: 4 bars at 09:31 = 09:15/20/25 closed + 09:30 forming), so the fourth guarantees `today[2]` is CLOSED. Three places already said 09:30 (scanner.yaml "Scan moment: 09:30 IST"; engine.py "inputs are all fixed by 09:30"; buckets.yaml `entry_start: 09:30` commented "differs from swing-indian's 09:45") — the 6 was the outlier. **MY FIRST INSTINCT — "delay the scan to 09:45" — WAS WRONG AND WOULD HAVE SILENTLY DAMAGED THE STRATEGY**: the entry window opens at the bar stamped 09:25 and `_MAX_SIGNAL_AGE_BARS=1` only acts on a pattern that is the last closed bar or the one before, so at 09:45 the 09:25 (age 3) and 09:30 (age 2) pattern bars are stale and permanently unreachable — amputating the front of a validated entry window while looking like a fix. The user asked for the exact entry logic before deciding, which is what caught it. (2) The empty cut was then CACHED for the day: `already_ran` counted ScannerSnapshot rows, and an all-`data_`-reason pass answered no question (could-not-evaluate ≠ did-not-qualify, the same lesson Decision 033 taught the scanner and this session taught the sizer). Now retries, **bounded at 5/session** — a retry costs ~230 symbols × 2 calls (measured 113s) against a 60s tick and Dhan's 5 req/s cap shared with swing-indian, so unbounded it would serialise into an hour of continuous scanning and manufacture the 2026-07-22 429 storm, i.e. the very blindness it detects. Extracted as pure `should_rescreen()` for testability; persistence was already delete-then-insert per (date, strategy_id), so re-running is idempotent. BLAST RADIUS CHECKED BEFORE CHANGING ANYTHING (user's instruction): one definition, one use site, nothing else in src/tests/scripts references it, and a complete NSE session is 75 5m bars so it cannot bind on a replayed day — **verified, not assumed**: `gap_reversal_parity.py` scores an identical 75/76 on all three axes before and after, same single accepted VEDL 2025-08-26 miss. 655 green (7 new), ruff clean. Commit 0ea1f43.
- 2026-08-10 (later) — **Two incidents, one of them mine.** (A) 15:15 IST: a SECOND Dhan session took the account and the bot's token — valid another 16h — was rejected mid-session. Not self-inflicted: the bot minted exactly twice all day (07:00 routine, 15:18:55 recovery) and nothing in between. Dhan is single-session, so a non-expired token being rejected means an external login (buckets.yaml already warns "do not log into Dhan while the bot runs"). Outage 15:15:49→15:18:55 = **186s, self-healed** — the identical eviction cost TWO DAYS on 08-04/05, so the Friday token fix is what turned it into three minutes. It still paged three sweeps because `_TOKEN_GRACE_SECONDS` was 180 and recovery took 186 — **beaten by six seconds**. Raised to 300: the 130s cooldown is server-side per CLIENT ID and the COMPETING login starts it, so the bot waits out someone else's two minutes plus its own tick; a genuinely stuck token still pages within 5 min. Also added `is_transient_upstream_error` (5xx + httpx.TransportError) on a 120s/two-sweep grace, because the 08-09 Dhan 502 recovered on the next tick and paged instantly. Kept NARROW — 4xx, DH-906 (own grace, checked first), and our own KeyError/ValueError all still page on the FIRST failure, because silencing the safety path is its own hazard. Commit 365e503. (B) **That deploy then took the bot DOWN for ~80 minutes.** The 18:00 IST restart landed inside Dhan's ~130s mint cooldown → startup probe served a rejected token → DH-906 → the broad `except` disabled the WHOLE Dhan account for the process → both Indian buckets are the only enabled ones → `runners` empty → `main()` returned NORMALLY → **systemd `Restart=on-failure` treats exit 0 as success** → service inactive, `NRestarts=0`, would have missed the next open with nothing recovering it. The alert text made it worse by blaming "bad creds or the sandbox edge-blocking this IP" — neither true, and the real cause was not in its list. Found only because the USER sent a screenshot. Restored by hand (`systemctl start`, 13:42 UTC) before fixing. BOTH links fixed in 0d21ab2: (1) `_probe_with_retry` rides out DH-906/5xx on a schedule that deliberately SPANS the mint cooldown (20/60/70s, pinned by a test asserting the sum > 130) while a PERMANENT fault (sandbox edge 403 on datacenter IPs, 07-12) still fails on the first attempt; (2) an empty runner set with buckets ENABLED now raises `SystemExit(1)` so systemd retries — an empty buckets.yaml is a legitimate config and still exits 0. (2) is the real net: any unanticipated init failure becomes a retry loop with an alert instead of a silently idle box. Nothing traded during either incident (market closed, zero open positions). 665 green, ruff clean. **LESSON: four deploys in one day, and each restart is a real risk event on this system because of the single-session token. Batch changes into ONE deploy and never deploy near 09:15 or 15:15.**
- 2026-08-12 — **The protective stop, fixed for real — after an adversarial review found my first attempt was inert.** User gave the one-time Dhan MTF consent (the 2026-08-11 root cause: MTF was *enabled*, but Dhan requires a separate consent checkbox ticked on a manual App/Web order; API orders cannot tick it, so every MTF order swing-indian ever sent was RMS-rejected — which is why it had never filled since going live 2026-07-27). User declined the async-RMS CNC fallback, approved the stop-attribution fix. A 4-lens workflow review of that fix found **it would not have worked**, and all three defects had to land together: (1) `_load_attribution` read only `Position` (reconciler writes it every 5 MIN; the sweep runs ~9 SEC after a fill) → falls back to the entry Trade now — **but my first draft filtered `[FILLED, PARTIAL, OPEN]` and a Dhan order is `pending` at placement** (manager writes PENDING then overwrites with the broker ack; Dhan maps TRANSIT *and* PENDING → pending; CASTROLIND still read PENDING a full minute after its fill), so the filter excluded the exact row the fallback exists to find. PENDING added. (2) **Attribution never controlled the product at all** — `ensure_stop_protection` called `place_order` with no `product=`, so `OrderRequest.product` was None and `DhanClient` fell back to its constructor default MTF for *every* stop; a reduce-only MTF sell against an INTRADAY long reduces nothing. Now plumbs bucket→product (`stop_products` in run_bot). (3) **The sweep could not see its own stops**: `plan_stop_protection` matches only when `reduce_only` is True and the Dhan adapter hardcoded it `False`, so `stops_by_symbol` was permanently empty → a fresh stop planned every 60s forever. Harmless only while every stop was rejected; **MTF consent made it dangerous**, since the next accepted stop would stack one per minute. Now inferred from trigger price + OUR `correlationId`, which Dhan returns only for API orders — so a stop the USER placed in the app can never match and can never be cancelled (Decision 027). Review also caught two errors in my own work: a docstring claiming that day's 15% came from a Trade (it was the `min()` bucket fallback — `gap_down_reversal` stamps no `stop_distance`), and a test whose name promised a null-bucket guarantee while passing `positions=[]`, so it built nothing and asserted a guarantee the code does not make; it now pins the real behaviour. NOT addressed: the 500-row Trade window, and swing-indian fresh fills moving 15%→20% once attribution works. NOTE: the review's `confirmed: []` was misleading — 23 of 27 agents died on a monthly spend limit, so verdicts were dropped, not refuted; findings came from reading `journal.jsonl` directly. 677 green, ruff clean. Commit 23aebf7.
- 2026-08-17 — **Decision 034: the stop now rides on the entry order (Dhan Super Order), placement half shipped behind a flag that is OFF.** Continues the 08-14 session, where the user asked "there is a concept of super order where entry sl, tp can be placed in one order — have you checked that?" and the answer was no: Decision 022 went straight to "rest a separate stop after the fill" and the premise was never questioned. Three of that week's four bugs were patches around a race this design does not have, and the fourth — PIIND opening and only THEN being found unprotectable — is the one that inverts: with the stop in the same request, an unplaceable stop means **the entry does not happen**. WHAT SHIPPED: `OrderRequest.attached_stop_price/.attached_target_price` + `Broker.supports_attached_stop/attached_stop_triggers/retire_attached_stop` (all defaulted so **Delta India and every crypto bucket are untouched**); `DhanClient` super-order placement, leg retirement and ownership; the entry path computing the stop at decision time; sweep coexistence; `check_stop_coverage` learning about legs; `attached_stops_enabled` (default False). THE TARGET LEG: Dhan makes `targetPrice` mandatory and neither strategy has a target, so it is placed then cancelled — but **not "far away"**, because an out-of-band price would reject the WHOLE super order, entry included. It goes just inside the circuit band (same `_BAND_SAFETY` machinery as the stop), and a failed cancel is stamped on the Trade, retried by the sweep, and paged: a surviving target is a live exit no backtest justifies (House Rule 7). THE CRUX — NAKED SHORT: a stop leg that outlives its position sells stock we no longer hold, which on MTF is a short, i.e. worse than the bug being fixed. Both bot close paths (`_close_position`, `_flatten_positions`) funnel through `place_order(reduce_only=True)` into the adapter, so retirement lives THERE — one chokepoint covering both and any written later — and is fail-CLOSED: if the leg cannot be cancelled, or the lookup that finds it fails, **the closing order is not sent** (refusing to sell is recoverable; selling twice is not). Paths a chokepoint cannot front-run (Dhan's 15:20 MIS auto-square-off, a manual close by the user, the target filling) are caught by a new orphan-leg pass in the sweep, which measures "held" as held BY THE BOT so a user's holding of the same scrip cannot keep our orphaned leg alive. THREE REAL DEFECTS FOUND BY A 6-AGENT COUPLING REVIEW, all in code I had already written this session: (1) a blocked exit raised into `OrderManager`'s generic handler → wasted day-book scan on the rate-limited shared token → **REJECTED row → `check_reject_rate` (3/15min) → bucket HALT for a reason unrelated to rejects**; now a contract-level `AttachedStopRetireError` marked CANCELED, because nothing was refused and nothing was even sent. (2) `_reconcile_orders` keyed `open_ids` on order id alone, and since the three legs SHARE one id with the stop leg resting open for the life of the position, **the filled BUY entry could never leave OPEN** — silently dropping it out of realized-P&L pairing, the tax ledger, EOD round-trips and the dashboard badge; now keyed on (id, side). (3) the coexistence skip popped a symbol's resting orders wholesale, which would have silently left a LEGACY standalone stop resting beside the new leg — two stops on one long is a double-sell; now the leftover is cancelled and paged, attached wins. ALSO FIXED, pre-existing and broker-agnostic: `_enrich_trades_pnl` bucketed fills by order id alone, so a stop-leg SELL would average into the entry's BUY and stamp a price describing no trade that ever happened; fills are now matched on side too. **NOT BUILT — and the gate on turning the flag on**: nothing yet writes an exit `Trade` row when a leg actually FIRES. `net_owned` decrements only on a FILLED SELL row, so a stopped-out position would leave `bot_owned_quantities` permanently inflated and Decision 027's scoping reads that number — the bot would believe it still holds shares it sold. The row's shape is settled (sentinel `{orderId}#SL`, `extra.reduce_only` + `protective_stop`); the DETECTION is blocked on venue behaviour that cannot be established offline (`GET /v2/super/orders` is documented as per-day and swing-indian holds for days). Guessing there is how the last four Dhan bugs happened, so it waits for one live super order. 741 green (38 new in `test_super_order.py`), ruff clean. The band-clamp test reproduces the real PIIND figure (2288.20) verified live on 08-12. NOT PUSHED.
- 2026-08-18 (cont.) — **Decision 034 ARMED LIVE on both Indian buckets, and a bigger gap found underneath it.** User chose both buckets over my recommendation of intraday-indian alone (recorded in the commit: intraday squares off at 15:15 so a leg cannot survive overnight, and it sends productType INTRADAY, so it does NOT exercise swing-indian's MTF path where every prior rejection lived). Verified on the VM: `ATTACHED_STOPS_ENABLED=True`, `TRADING_MODE=live`, both buckets `attached_stops=True`. **CI EARNED ITS KEEP:** `deploy.sh` refuses any SHA whose checks are not green, so the VM stayed on `4f63bc0` and neither broken commit reached the live bot. What it caught was mine and is worth remembering — `TestFeatureGating` executes the real `ensure_stop_protection`, whose loaders query Postgres, and the local `.env` points at the **LIVE production database**; both tests passed here by reading prod and failed in CI's empty SQLite with `no such table: trade`. A green local run is not evidence for any DB-touching test; re-verify with `DATABASE_URL=sqlite:///...` (now a standing rule). Also shipped: `check_stop_coverage` now credits a stop the bot did NOT place, gated on SELL + unfilled size >= the bot's holding — a hand-placed stop returns `correlationId "NA"`, so ownership (right for CANCELLING, per the 08-14 bug) was wrongly gating a question about whether the position is protected; without it the user could protect PIIND and the bucket would halt every tick anyway. **STATE AT HANDOFF:** intraday-indian kill switch cleared by the user (its halt was stale — PPLPHARMA gone since 08-14); swing-indian deliberately left ENGAGED until the user's manual PIIND stop lands (SELL qty>=15), because clearing it first just re-halts within two ticks. Ledger drift confirmed live and now self-correcting via `_detect_unrecorded_exits` (it claimed 267 CASTROLIND the user does not hold). **THE FIND:** chasing "can a super-order SL leg live multiday", `_order_body` sends `validity: DAY` on every standalone protective stop — so Decision 022's whole premise ("protects even when the bot is down") has been FALSE OVERNIGHT since swing-indian went live 2026-07-27, independent of super orders; the user's own hand-placed PIIND stop from 08-14 was gone by 08-18. Sized honestly: an overnight stop would not beat a gap either (it triggers at the open), so the real exposure is bot/VM dead at the open while the stock slides. Dhan's Forever Order API explicitly allows productType CNC **and MTF** — written up as **Decision 035, PROPOSED/NOT BUILT**, deliberately gated on one observation: `_super_order_body` sends no `validity` at all, so if a super-order leg survives overnight on MTF, 035 is unnecessary. Watch swing-indian's first MTF super order across a session boundary before building anything. 754 green, ruff clean, deployed at `7ad7708`.
- 2026-08-18 (later) — **Every Dhan P&L figure has been gross of charges since the integration existed; now fixed.** User asked why the bot cannot get actual fees from Dhan. It can — the adapter reads the wrong endpoint. `get_fills` uses `/v2/trades`, the intraday DAY BOOK, which reports executions and no costs, and hardcodes `commission=Decimal("0")` with a comment deferring it to "the ledger" that was never built. The costs live on `/v2/trades/{from}/{to}/{page}`, the trade-history report, which returns them per fill as `brokerageCharges`, `stt`, `exchangeTransactionCharges`, `sebiTax`, `stampDuty`, `serviceTax` (GST under its pre-2017 name) — no aggregated total, you sum the six. NOT COSMETIC: `realized_pnl` already subtracts `entry.fees + exit.fees`, it has simply been subtracting nothing, so the dashboard, EOD report, tax ledger and edge stats (profit factor, win rate) have all been gross. swing-indian's first closed round trip (PIIND, +₹195.98 net of ₹66.52 MTF carry) is the first number this actually distorts, and for a strategy with a ~0.62% backtested mean trade, round-trip charges are a large fraction of the edge — the difference between measuring it and flattering it. SHIPPED: `OrderCharges` on the broker contract (breakdown kept separate, not one number — STT and stamp duty are taxes, brokerage is negotiable, and a filing needs them apart) + `Broker.get_order_charges(start, end)` defaulting to `{}` so Delta/crypto is untouched; `DhanClient.get_order_charges` walking the paginated report until a page comes back empty (a truncated read silently UNDER-reports cost, which reads as a cheaper trade); `Reconciler._enrich_trade_charges` running BEFORE `_enrich_trades_pnl` in the same pass. TWO RULES CARRY THE SAFETY, both pinned by test: (1) **a zero total is "not computed yet", not "free"** — brokers bill at end of day and STT alone is non-zero on both legs of a delivery trade, so accepting a zero would stamp `charges_final` and bake the placeholder in permanently, i.e. re-create the exact bug; (2) **the un-stamp**, because charges necessarily arrive AFTER P&L was finalised with zero fees — it clears both legs including the entry's `closed_by_trade_id`, since the pairing loop skips entries already marked closed and leaving it would strand the exit permanently unpairable with NO P&L, worse than the gross figure being corrected. Cannot loop: `charges_final` is written in the same transaction. Synthetic exits are skipped (never an order, nothing billed). 767 green (13 new in `test_trade_charges.py`), ruff clean, verified CI-style against empty sqlite. NOT yet exercised against Dhan — charges for a given day only appear at EOD, so the first real enrichment is tonight's pass over today's PIIND round trip.
- 2026-08-18 (later still) — **The phantom short: a sale artifact that nearly made the risk-reducing module OPEN a position.** swing-indian exited PIIND at 12:16 IST (its first ever complete round trip, +₹262.50 gross). Selling stock out of HOLDINGS shows as a negative day-position in Dhan's `/v2/positions` until settlement, so the broker reported PIIND **short 15** for a few minutes. The exit order was still PENDING and `net_owned` only decrements on a FILLED sell, so the symbol still looked owned and passed the shared-account guard. Three layers then acted on the artifact: (1) the stop sweep read side=short and planned a BUY stop at 2714.60 ABOVE the market — Dhan rejected it, and had it been ACCEPTED, triggering it would have BOUGHT 15 shares, i.e. the module whose entire job is reducing risk opening a position with no strategy behind it; (2) the reconciler adopted it as a short Position row (`orphan_position_reopened`); (3) **the path with no backstop** — that row is visible to `BucketRunner._run_exits`, which computes the closing side as BUY and would have purchased 15 shares to "close" a position that does not exist, and exits carry `allow_when_killed=True` (Decision 024) so the kill switch would NOT have stopped it. THE UNIFYING RULE, applied at all three sites: `net_owned` returns only positive (long) nets by construction, so on a SHARED account a short can never be proven ours — not ours ⇒ do not touch. `plan_stop_protection` skips shorts (crypto untouched: exclusive sub-accounts pass `owned_quantities=None`, and Delta shorts are legitimate — pinned by test); `_reconcile_positions` refuses to adopt one AND flattens any existing short row (cleanup plus containment, since that row is what feeds the exit engine); `_run_exits` drops short rows in Indian buckets, because the reconciler sweeps on its own 5-minute clock and the bucket ticks faster. 772 green (5 new in `test_phantom_short.py`), ruff clean, verified CI-style. Note the stale row from today is cleaned automatically by the new Case 0 on the next reconcile pass.
