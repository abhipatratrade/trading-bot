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
- [ ] `pyproject.toml` (Python ≥ 3.11, deps pinned)
- [ ] `.gitignore`
- [ ] `.env.example`
- [ ] `railway.toml` — service definitions
- [ ] `README.md`
- [ ] Initial git commit

### 0.2 Core plumbing
- [ ] `src/core/config.py` — pydantic Settings, env-driven, TRADING_MODE switch
- [ ] `src/core/logging.py` — structured JSON logs + secret redaction filter
- [ ] `src/core/db.py` — SQLAlchemy engine + session factory
- [ ] `src/core/models.py` — `Trade`, `Position`, `AuditLog`, `KillSwitch`,
      `StrategyParamChange`, `DailyUniverse`, `ScannerSnapshot`,
      `SymbolMapping`
- [ ] `src/core/clock.py` — injectable clock (real / fake) for tests
- [ ] Alembic init + first migration

### 0.3 Broker layer (testnet only in this phase)
- [ ] `src/brokers/base.py` — `Broker` ABC (place_order, cancel, positions, balances)
- [ ] `src/brokers/delta_india/client.py` — REST + HMAC signing (testnet)
- [ ] `src/brokers/delta_india/ws.py` — WebSocket: positions, fills, ticker
- [ ] Smoke script: place + cancel a testnet order from CLI

### 0.4 Data sources
- [ ] `src/data_sources/base.py` — `MarketData` interface
- [ ] `src/data_sources/binance.py` — public WS + REST (no auth)
- [ ] `src/data_sources/delta_india.py` — public market data
- [ ] Symbol mapping loader (CSV → `symbol_mapping` table)

### 0.5 Order manager + reconciler
- [ ] `src/order_manager/manager.py` — idempotent placement (`client_order_id`)
- [ ] `src/order_manager/reconciler.py` — DB ↔ exchange diff at startup + every 5 min

### 0.6 Safety
- [ ] `src/safety/kill_switch.py` — DB-flag check, called every loop
- [ ] `src/safety/breakers.py` — daily DD, liquidation distance, funding extreme
- [ ] Dashboard kill-switch button writes to DB

### 0.7 Dashboard skeleton
- [ ] `src/dashboard/app.py` — FastAPI + HTMX shell
- [ ] Pages: positions, recent trades, kill switch, params snapshot, CSV export

### 0.8 Scheduler + nightly export
- [ ] `src/entrypoints/run_scheduler.py`
- [ ] Nightly job: dump trades to Parquet + CSV → Google Drive folder
- [ ] Telegram alert wiring (env-gated, no-op if no token)

### 0.9 Railway provisioning (USER does this part interactively)
- [ ] User: create Railway project
- [ ] User: provision Postgres
- [ ] User: set env vars (DELTA_TESTNET_*, BINANCE_PUBLIC_*, KITE_*, TELEGRAM_*, GDRIVE_*)
- [ ] Deploy 3 services: bot-worker, dashboard, scheduler
- [ ] Verify all 3 boot, dashboard reachable, kill switch flippable

**Phase 0 exit criterion**: bot-worker boots on testnet, reconciles cleanly,
dashboard shows kill switch, scheduler runs nightly export. **No real strategy yet.**

---

## Phase 1 — Crypto Long-term [priority 1]

Strategy: 1D timeframe, 5x leverage, top-5 by Delta India 24h volume,
equal-weight, daily rebalance.

- [ ] `src/strategies/crypto_longterm/policy.yaml` (v1, with backtest_ref)
- [ ] `src/strategies/crypto_longterm/runner.py`
- [ ] Volume scanner: rank Delta India perps by 24h notional, take top 5
- [ ] Filter: only symbols with a Binance equivalent (signal feed available)
- [ ] Daily rebalance loop: close non-universe, open new at equal weight × 5x
- [ ] Strategy-specific safety wrapper
- [ ] **Run on testnet ≥ 14 days unattended before going live**
- [ ] Go live with ₹50,000 capital

**Phase 1 exit criterion**: 14 testnet days clean + first live week clean.

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
