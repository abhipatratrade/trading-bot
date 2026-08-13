# Graph Report - .  (2026-08-13)

## Corpus Check
- 256 files · ~172,110 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 3330 nodes · 8482 edges · 196 communities (179 shown, 17 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 963 edges (avg confidence: 0.58)
- Token cost: 802,427 input · 0 output

## Community Hubs (Navigation)
- ORM Models & EOD Reporting
- Gap Reversal Parity & Data Source ABC
- DB Session, Migrations & Ops Scripts
- Bot Entrypoint, Alerts & Clock
- Scanner Engine & Filters
- Logging & Dry-Run Harnesses
- Mean Reversion 1h Tests
- Locked Architecture Decisions
- Bucket & Strategy Master Schema
- Tax Ledger & Trade Export
- Dashboard Bucket Routes
- Order Manager & Stop Protection
- Delta India Broker Client
- Mean Reversion Scan Engine
- Dhan Broker Client
- Intraday Indian Bucket Config
- Delta Market Data & Symbol Sync
- Dashboard HTML Templates
- Standalone Dhan Scanner Tool
- Dhan Auth Test Fixtures
- Transient vs Fatal Error Classification
- Dhan Market Data Adapter
- Session Invariant Enforcement
- Regime Retrain Job
- Gap Reversal Screen Tests
- Secret Redaction in Logs
- Dhan Broker Order Tests
- Archive Retention & Cutoff
- Swing Bucket Config YAML
- Dhan Token Store (Postgres)
- Slippage Decomposition
- Position Reconciler
- Dhan Token Mint & Cooldown
- Dhan Token Manager Lifecycle
- Strategy Stop Distance & Carry
- EOD Digest Rendering
- Scanner Bar Key Tests
- Gaussian HMM Wrapper
- Technical Indicators & Parity
- Google Drive Archive Export
- Realized P&L Math
- Stop Protection Planning Tests
- Bucket Runner Margin Fitting
- Candlestick Pattern Flags
- Bot-Owned Position Scoping
- EOD Edge & Slippage Payload
- Regime Model Verification
- NSE Market Calendar
- EOD Report Sections
- Notional-to-Contracts Sizing
- Strategy Exit Selection
- Broker Interface ABC
- Equity Circuit Breakers
- Notional & Reject-Rate Invariants
- EMA 9/15 Crossover Strategy
- Square-Off & Liveness Invariants
- Equity Scanner Daily Pass
- Delta Client Retry Hardening
- Dhan Data Fetch Tests
- Settings & Binance Data
- Broker Account Config
- Archive Lag Watchdog
- Regime Feature Computation
- HMM Fit & Predict Round-Trip
- Blind Scanner Coverage Check
- Phase 4 Stocks Swing Milestones
- Net Owned Quantity
- Kelly Fraction Math
- CI Deploy Gate & Bucket Registry
- Blind Scan Outage & EOD Tier 3
- Alert Dedup & Recovery
- Dhan Rate Limit Retry
- Live Mode Credential Validation
- EOD Markdown Formatting
- Allocation Caps
- Phase Build Tracker
- Delta WebSocket Client
- Binance Market Data
- Blasting Momentum (inert)
- Dashboard App & CSRF
- Foreign Position Invariant
- Bucket Config Loader
- Stop Attribution Merge
- Locked Decisions Table
- Journal Markdown-to-HTML
- Overnight Position Split
- Broad Gap Reversal Rescreen
- Stop Place Retry Budget
- Scheduler Nightly Jobs
- swing-indian Live Bucket
- Regime Persistence Diagnostic
- House Rules
- Phase 0/1 Foundations
- Regime Model Store
- Journal Export Watermark
- Signal Delivery Invariant
- Tick Cadence by Timeframe
- Stop Clamp Tightening Only
- Project Bible & North Star
- Kill Switch & Runbook
- Broker Sizing Primitives
- Clock Abstraction
- Regime Window Anchoring
- 8-Step Bucket Pipeline
- Heartbeat & Archive Watermark
- Heartbeat Staleness
- Dashboard Basic Auth
- Fill Aggregation
- Bucket Cumulative P&L
- Dedup Window by Timeframe
- Sizing Equity Source
- Entrypoint Import Smoke Tests
- Reconciler Bucket Scoping
- Square-Off Invariant
- Profit Factor
- Dhan Sandbox vs Live Config
- Exchange Side Mapping
- IST Day Bounds
- Strategy Entry Interface
- Dhan Stop Order Recognition
- Signal Delivery Notice
- Dhan Symbol Resolution
- Dhan HTTP Fakes
- Migration 0002 Buckets
- Stop Trigger Price
- Pattern Series Helpers
- Migration 0012 Bar Key
- Deploy Script
- Win Rate
- EMA Strategy Exits
- ORM Column Parity Test
- Dhan Setup Script
- Reporting Package Init
- Legacy Scanner Package
- Pending Order Attribution
- Stop Product Propagation
- Stop Protection Fragment
- Decision 001 Python
- Decision 002 Railway Hosting
- Orphan Decision Node A
- Decision 010 Telegram Alerts
- Orphan Decision Node B
- Orphan Decision Node C
- Project Metadata (pyproject)

## God Nodes (most connected - your core abstractions)
1. `session_scope()` - 96 edges
2. `AuditLog` - 83 edges
3. `OHLCVBar` - 77 edges
4. `MarketRegime` - 66 edges
5. `DhanTokenManager` - 63 edges
6. `OrderStatus` - 60 edges
7. `Broker` - 57 edges
8. `Trade` - 57 edges
9. `AuditEventType` - 56 edges
10. `MarketData` - 55 edges

## Surprising Connections (you probably didn't know these)
- `README claim: equities via Zerodha Kite (stale vs Decision 012)` --conceptually_related_to--> `Locked Decisions table`  [AMBIGUOUS]
  README.md → CLAUDE.md
- `Tool sits OUTSIDE the deterministic bot loop` --conceptually_related_to--> `House Rules (non-negotiable)`  [INFERRED]
  scripts/dhan-scanner/README.md → CLAUDE.md
- `Indicators imported from BACKTEST_ENGINE_DIR for faithfulness` --semantically_similar_to--> `Same code path for backtest and live`  [INFERRED] [semantically similar]
  scripts/dhan-scanner/README.md → CLAUDE.md
- `MTF eligibility + funding risks (silent 1x degrade)` --semantically_similar_to--> `fallback_product CNC (1x degrade instead of skipping)`  [INFERRED] [semantically similar]
  scripts/dhan-scanner/README.md → buckets.yaml
- `Fail-soft Dhan init (crypto buckets keep running)` --semantically_similar_to--> `TRADING_MODE is process-wide (why longterm-crypto is paused)`  [INFERRED] [semantically similar]
  docs/runbook.md → buckets.yaml

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Seven capital buckets registered in buckets.yaml** — buckets_bucket_registry, buckets_longterm_crypto, buckets_swing_crypto, buckets_scalp_crypto, buckets_gambling_crypto, buckets_longterm_indian, buckets_swing_indian, buckets_intraday_indian, claude_bucket_architecture [EXTRACTED 1.00]
- **Push-to-live deploy gate flow (CI → deploy.sh → selfcheck → restart → Telegram)** — _github_workflows_ci_ci_workflow, _github_workflows_ci_lint_step, _github_workflows_ci_unit_tests_step, docs_runbook_ci_deploy_gate, docs_runbook_vm_auto_deploy, docs_runbook_selfcheck, docs_runbook_telegram_deploy_alerts [EXTRACTED 1.00]
- **swing-indian go-live stack (config, ops, retired bridge tool, stop policy)** — buckets_swing_indian, docs_runbook_swing_indian_ops, docs_runbook_dhan_prepare_timer, scripts_dhan_scanner_readme_interim_dhan_scanner, claude_swing_indian_strategy_decision, claude_protective_stops [INFERRED 0.85]
- **num3 Formatting Filter Consumers** — src_dashboard_templates_bucket_money_panel, src_dashboard_templates_bucket_running_trades, src_dashboard_templates_bucket_recent_trades, src_dashboard_templates_buckets_overview_bucket_card, src_dashboard_templates_partials_positions_table_positions_table, src_dashboard_templates_partials_trades_table_trades_table [EXTRACTED 1.00]
- **Three Params Views Per Bucket** — src_dashboard_templates_params_params_index, src_dashboard_templates__params_header_params_view_tabs, src_dashboard_templates_params_allocation_allocation_view, src_dashboard_templates_params_trading_trading_view, src_dashboard_templates_params_scanner_scanner_view [EXTRACTED 1.00]
- **Duplicated Bucket Identity Summary Line** — src_dashboard_templates__params_header_bucket_header, src_dashboard_templates_bucket_bucket_header, src_dashboard_templates_params_params_index, src_dashboard_templates_buckets_overview_bucket_card [INFERRED 0.95]
- **Defense-in-depth neutralization of the regime gate (Decision 029)** — src_strategies_intraday_indian_regime_regime_config, src_strategies_intraday_indian_regime_regime_gate_disabled_by_design, src_strategies_intraday_indian_allocator_flat_regime_multipliers, src_strategies_intraday_indian_allocator_allocator_config, src_strategies_intraday_indian_allocator_broad_allocator_config [EXTRACTED 1.00]
- **intraday-indian morning cut: engine, filters, ranker, cached universe** — src_strategies_intraday_indian_scanner_equity_intraday_engine, src_strategies_intraday_indian_scanner_gap_down_pct, src_strategies_intraday_indian_scanner_corporate_action_guard, src_strategies_intraday_indian_scanner_first15_body_atr_frac, src_strategies_intraday_indian_scanner_gap_down_abs_desc, src_strategies_intraday_indian_scanner_daily_universe_cache [EXTRACTED 1.00]
- **Two named scanner+allocator sets sharing one capital pool (Decision 026)** — src_strategies_intraday_indian_scanner_scanner_config, src_strategies_intraday_indian_allocator_allocator_config, src_strategies_intraday_indian_scanner_broad_scanner_config, src_strategies_intraday_indian_allocator_broad_allocator_config, src_strategies_intraday_indian_scanner_broad_shared_capital_slot_contention [INFERRED 0.85]
- **Per-bucket scanner + regime + allocator config triple** — src_strategies_scalp_crypto_scanner_config, src_strategies_scalp_crypto_regime_config, src_strategies_scalp_crypto_allocator_config, src_strategies_swing_crypto_scanner_config, src_strategies_swing_crypto_regime_config, src_strategies_swing_crypto_allocator_config, src_strategies_swing_indian_scanner_config, src_strategies_swing_indian_regime_config, src_strategies_swing_indian_allocator_config [INFERRED 0.85]
- **swing-indian regime gate neutralised in three independent places** — src_strategies_swing_indian_regime_config, src_strategies_swing_indian_regime_gate_removes_edge, src_strategies_swing_indian_allocator_flat_regime_multipliers, src_strategies_swing_indian_allocator_config [EXTRACTED 1.00]
- **Blasting Momentum parked configuration set (scanner + allocator + dormant regime model)** — src_strategies_swing_indian_scanner_blasting_config, src_strategies_swing_indian_allocator_blasting_config, src_strategies_swing_indian_scanner_blasting_inert, src_strategies_swing_indian_regime_niftybees_history [EXTRACTED 1.00]
- **Layered safety: stops, breakers, kill switch, invariants** — docs_decisions_022_broker_side_protective_stop, docs_decisions_023_daily_anchored_drawdown_breaker, docs_decisions_024_kill_switch_semantics, docs_decisions_033_session_invariants, docs_decisions_authority_ladder, docs_decisions_stop_protection_sweep [EXTRACTED 1.00]
- **Evolution of the Kelly sizing base** — docs_decisions_015_kelly_on_bucket_capital, docs_decisions_018_kelly_margin_check, docs_decisions_025_kelly_on_live_equity, docs_decisions_027_indian_capped_sizing_equity, docs_decisions_sizing_equity, docs_decisions_committed_margin_budget [EXTRACTED 1.00]
- **Consequences of one shared Dhan account (no sub-accounts)** — docs_decisions_012_stocks_broker_dhan, docs_decisions_027_indian_capped_sizing_equity, docs_decisions_030_intraday_indian_universe_mis_capital, docs_decisions_031_cnc_fallback_one_x, docs_decisions_bucket_ledger_pnl, docs_decisions_effective_holdings [EXTRACTED 1.00]
- **Tier 1 session invariant suite and its authority ceiling** — docs_phases_session_invariants, docs_phases_invariant_squareoff, docs_phases_invariant_stop_coverage, docs_phases_invariant_bucket_liveness, docs_phases_invariant_foreign_positions, docs_phases_invariant_scan_coverage, docs_phases_invariant_signal_delivery, docs_phases_authority_ladder, docs_phases_invariant_audit_trail [EXTRACTED 1.00]
- **Dhan integration failure chain that kept the Indian buckets from ever trading** — docs_phases_dhan_token_single_session, docs_phases_dhan_client_id_bug, docs_phases_mtf_consent_blocker, docs_phases_blind_scan_outage, docs_phases_min_session_bars_bug, docs_phases_empty_runner_exit0, docs_phases_dhan_rate_limit_pacing [INFERRED 0.85]
- **Indian equity strategy stack: scanners, strategies and frozen-trade parity harnesses** — docs_phases_meanrev_scanner, docs_phases_mean_reversion_1h_strategy, docs_phases_meanrev_parity_harness, docs_phases_gap_reversal_scanner, docs_phases_gap_down_reversal_strategy, docs_phases_gap_reversal_parity, docs_phases_patterns_module [EXTRACTED 1.00]

## Communities (196 total, 17 thin omitted)

### Community 0 - "ORM Models & EOD Reporting"
Cohesion: 0.07
Nodes (82): AuditEventType, AuditLog, BrokerName, OrderSide, OrderStatus, Position, PositionSide, StrEnum (+74 more)

### Community 1 - "Gap Reversal Parity & Data Source ABC"
Cohesion: 0.05
Nodes (54): first_pattern(), load(), main(), Parity check: does this repo's port reproduce the frozen gap-reversal backtest?…, (pattern_name, entry_bar_hhmm) for the first hit in the entry window., main(), intraday-indian DRY RUN — what would this bucket trade, right now? Runs the…, FundingRate (+46 more)

### Community 2 - "DB Session, Migrations & Ops Scripts"
Cohesion: 0.05
Nodes (64): DeclarativeBase, Alembic environment. Reads ``DATABASE_URL`` from the running process…, Generate SQL without a live DB connection., Connect and apply migrations., run_migrations_offline(), run_migrations_online(), Materialise stored session reports into ``docs/journal/YYYY-MM-DD.md`` for git.…, Record a capital adjustment for a bucket's cumulative P&L baseline. The… (+56 more)

### Community 3 - "Bot Entrypoint, Alerts & Clock"
Cohesion: 0.05
Nodes (65): note_alert_recovery(), note_sustained_recovery(), Telegram alert sender — env-gated, no-op if no token. Usage:: from…, Signal that ``key``'s condition has cleared. If the key fired at least once in…, Clear ``key``'s sustained-failure state. Sends a one-off recovery ``message``…, Send a Telegram message. Returns True on success, False if disabled or failed., Send an alert, capped at ``max_count`` pings per ``window_seconds``. Within a…, send_alert() (+57 more)

### Community 4 - "Scanner Engine & Filters"
Cohesion: 0.06
Nodes (71): FilterFn, RankFn, DailyUniverse, The lean read-side of the scanner: today's chosen N symbols per strategy. The…, The full audit row: every coin the scanner evaluated, with metrics and filter…, Crosswalk between broker-specific symbol names. Scanner uses ``listed_on_delta…, ScannerSnapshot, SymbolMapping (+63 more)

### Community 5 - "Logging & Dry-Run Harnesses"
Cohesion: 0.05
Nodes (58): BoundLogger, main(), Build (and optionally send) an end-of-day session report by hand. The scheduler…, main(), _bars_with_cross_up(), FakeMarketData, _import_strategy_module(), main() (+50 more)

### Community 6 - "Mean Reversion 1h Tests"
Cohesion: 0.08
Nodes (66): bar_key(), evaluate(), Fresh downward cross of the −threshold band on the last complete 1h bin.…, _bar(), _cfg(), _daily(), _dislocated_series(), _last_day() (+58 more)

### Community 7 - "Locked Architecture Decisions"
Cohesion: 0.05
Nodes (65): Decision 004 — Binance signals, Delta India execution, Decision 005 — Stocks via Zerodha Kite Connect (superseded), Decision 006 — Strategy params: YAML in git, not DB, Decision 007 — Postgres truth + nightly Google Drive mirror, Decision 008 — Deterministic core, agentic perimeter, Decision 009 — Backtest engine out of scope for this repo, Decision 012 — Stocks broker: switch Zerodha to Dhan, Decision 013 — Six (type x market) buckets with isolated capital (+57 more)

### Community 8 - "Bucket & Strategy Master Schema"
Cohesion: 0.06
Nodes (37): field_validator, BucketConfig, BucketsConfig, BaseModel, Top-level ``buckets.yaml``., Per-bucket config block from ``buckets.yaml``., load_strategy_master(), Exception (+29 more)

### Community 9 - "Tax Ledger & Trade Export"
Cohesion: 0.07
Nodes (50): _fmt(), main(), Write the consolidated bot-trade ledger to CSV (opens directly in Excel).…, Decimals as plain strings — no scientific notation, no float drift., build_ledger(), _dec(), financial_year(), fy_bounds() (+42 more)

### Community 10 - "Dashboard Bucket Routes"
Cohesion: 0.06
Nodes (45): FastAPI, Per-(bucket, symbol) regime prediction over time. One row per Brain inference.…, RegimeSnapshot, FastAPI + HTMX dashboard — read-only UI with kill-switch control. All routes…, _age_minutes(), bucket_detail(), _bucket_pnl(), buckets_overview() (+37 more)

### Community 11 - "Order Manager & Stop Protection"
Cohesion: 0.07
Nodes (39): Broker, Abstract broker interface. Implementations wrap a single exchange's REST API.…, Release resources. Default no-op; override if needed., TimeInForce, KillSwitchScope, KillSwitchEngagedError, make_client_order_id(), _map_broker_status() (+31 more)

### Community 12 - "Delta India Broker Client"
Cohesion: 0.08
Nodes (18): CancelResult, OpenOrder, Fetch a single order by exchange ID. Returns None if not found., Fetch a single order by client_order_id. Returns None if not found. Used by the…, DeltaIndiaClient, Any, Decimal, Response (+10 more)

### Community 13 - "Mean Reversion Scan Engine"
Cohesion: 0.08
Nodes (41): main(), swing-indian DRY RUN — what would the 1h mean-reversion bucket trade right now?…, bin_index(), daily_atr(), evaluate_with_reason(), ist_date(), ist_dt(), last_complete_bar_key() (+33 more)

### Community 14 - "Dhan Broker Client"
Cohesion: 0.08
Nodes (20): BalanceInfo, PositionInfo, Dhan access-token manager — TOTP auto-refresh. Dhan capped directly-generated…, DhanClient, _is_invalid_token(), _parse_ts(), Any, datetime (+12 more)

### Community 15 - "Intraday Indian Bucket Config"
Cohesion: 0.07
Nodes (44): gambling-crypto Allocator Config (stub), gambling-crypto Regime Config (stub, disabled), gambling-crypto Scanner Config (stub), intraday-indian Allocator Config (NIFTY-100 Gap-Down Reversal), backtest_baseline (HOLDOUT fold, reporting only), intraday-indian BROAD Allocator Config, Inherited NIFTY-100 mu/sigma Placeholder, Per-Symbol Cap Binds, Kelly Only Confirms (+36 more)

### Community 16 - "Delta Market Data & Symbol Sync"
Cohesion: 0.08
Nodes (28): main(), DeltaIndiaData, Any, Return raw product list from ``GET /v2/products``., Return ``[{symbol, baseAsset}, ...]`` for active perpetuals., Synchronous REST client for Delta Exchange India public data., _apply_training_overrides(), fetch_mappings() (+20 more)

### Community 17 - "Dashboard HTML Templates"
Cohesion: 0.10
Nodes (41): Shared Params Bucket Header, Allocation / Trading / Scanner Tab Strip, Base Layout Template, Dark Theme Design Tokens, HTMX 2.0.4 Runtime, Global Navigation Bar, Bucket Detail Page, Bucket Detail Header (+33 more)

### Community 18 - "Standalone Dhan Scanner Tool"
Cohesion: 0.12
Nodes (39): _bars_to_df(), _cancel_order(), _charts(), cmd_manage(), cmd_prepare(), cmd_scan(), cmd_status(), _df_to_bars() (+31 more)

### Community 19 - "Dhan Auth Test Fixtures"
Cohesion: 0.12
Nodes (37): _fake_jwt(), _FakeStore, _FlakyHttp, _mgr(), _mgr_cached(), _mgr_remote(), _Path, Dhan TOTP token manager (Phase 3/4 — src/brokers/dhan/auth.py). (+29 more)

### Community 20 - "Transient vs Fatal Error Classification"
Cohesion: 0.07
Nodes (36): BaseException, DhanAPIError, is_invalid_token_error(), is_transient_upstream_error(), Exception, True when ``exc`` is Dhan being briefly unwell, not the bot being wrong. A 5xx…, Raised when the Dhan API returns an error envelope or non-2xx status., True when ``exc`` is a Dhan single-session token invalidation (DH-906). Used by… (+28 more)

### Community 21 - "Dhan Market Data Adapter"
Cohesion: 0.08
Nodes (17): DhanData, Any, Client, Decimal, Path, The shared token manager — reused by the Dhan broker in live mode (live orders…, All tradeable NSE+BSE equity tickers in the resolved universe., Public ticker → ``(security_id, exchange_segment)`` resolver. Shared with the… (+9 more)

### Community 22 - "Session Invariant Enforcement"
Cohesion: 0.11
Nodes (36): enforce_session_invariants(), InvariantResult, Alert on violations and halt the offending bucket. Returns those halted. HALT…, Stands in for kill_switch + alerts so nothing touches a DB or Telegram., One uncovered reading can race a just-placed stop; two cannot., The 2026-07-28 bug: foreign_positions paged ~72x a day. send_alert_dedup's…, Silence is for an UNCHANGED condition — new content is news., Suppression must not outlive the condition that caused it. (+28 more)

### Community 23 - "Regime Retrain Job"
Cohesion: 0.10
Nodes (35): load_regime_config(), BaseModel, Path, Validated shape of ``regime.yaml`` per bucket. Fields: symbols: list of symbols…, RegimeConfig, _alert_summary(), _crypto_symbols_to_train(), _delta_to_binance() (+27 more)

### Community 24 - "Gap Reversal Screen Tests"
Cohesion: 0.10
Nodes (36): _cfg(), _daily(), Gap-down reversal (intraday-indian) — patterns, morning screen, strategy.…, 5m bars spanning a prior session + today's open at the requested gap., Long-only: the short side had no edge and is never scanned., Body below 25% of daily ATR (~2% here) = indecisive open, no trade., A split adjusts daily history but never the intraday series., No 09:15 bar ⇒ we can't measure the gap; skip rather than guess. (+28 more)

### Community 25 - "Secret Redaction in Logs"
Cohesion: 0.08
Nodes (34): EventDict, Any, LogRecord, structlog processor that redacts secrets from event dicts., Redact one stdlib log arg, preserving its type unless it holds a secret. Third-…, Applies the same redaction to STDLIB log records. ``_redact_processor`` only…, _redact_arg(), _redact_processor() (+26 more)

### Community 26 - "Dhan Broker Order Tests"
Cohesion: 0.18
Nodes (31): OrderRequest, Broker-agnostic order intent. Built by the order manager., _client(), _FakeHttp, Dhan broker client — order build, MTF fallback, stops, parsing (Phase 3/4)., No ``fallback_max_size`` ⇒ the rejection stands, never an uncapped retry., Routes by ``"<METHOD> <suffix>"`` to queued responses; records calls., _resolve() (+23 more)

### Community 27 - "Archive Retention & Cutoff"
Cohesion: 0.08
Nodes (35): mark_audit_archived(), date_, Advance the watermark to ``day``. Returns whether it moved. Never raises. The…, audit_cutoff(), datetime, How far back audit rows may be deleted. PURE. ``None`` means delete nothing:…, The audit log is never deleted ahead of its archive. ``audit_log`` is the…, THE bug this guard exists to prevent. A system with three months of unarchived… (+27 more)

### Community 28 - "Swing Bucket Config YAML"
Cohesion: 0.07
Nodes (35): Stub allocator must get real mu/sigma from the backtester before enabling, scalp-crypto Allocator Config (stub), scalp-crypto Regime Config (stub, disabled, BTCUSDT 5m proxy), scalp-crypto Scanner Config (stub, top-5 by 24h volume), Annualized-vol to per-hour mu/sigma derivation (divide by 8760, sqrt for sigma), swing-crypto Allocator Config (EMA 9/15, Kelly 0.25), Delta India contract-size table and fixed FX 85 INR/USD (swing-crypto), Placeholder mu/sigma must be replaced by backtester output before Phase 2 go-live (Decision 006 backtest_ref) (+27 more)

### Community 29 - "Dhan Token Store (Postgres)"
Cohesion: 0.08
Nodes (22): Engine, sessionmaker, PostgresTokenStore, Postgres-backed shared Dhan token store — the cross-VM peer source. The bot's…, Read/write the shared ``dhan_token`` row for one client id. ``minted_by`` is…, Return the row's token, or None on a miss OR any DB error. Never raises — a DB…, Upsert the shared token for this client id. Never raises. Uses INSERT ... ON…, get_engine() (+14 more)

### Community 30 - "Slippage Decomposition"
Cohesion: 0.12
Nodes (32): cost_bps(), decompose(), mean_bps(), Decimal, Execution slippage — what the strategy saw vs what we actually got. Both live…, Split the signal→fill gap into lag and execution. PURE. Each leg is computed…, Average of the known values. None when nothing is known. PURE., Parse a JSONB-stored price string. None on anything unusable. Prices ride in… (+24 more)

### Community 31 - "Position Reconciler"
Cohesion: 0.10
Nodes (20): _calendar_days(), _decimal_or_none(), _map_status(), Any, Decimal, Trade, DB ↔ exchange reconciler. Runs at startup and every 5 minutes to catch…, Extra WHERE clauses restricting Trade rows to this account's buckets. (+12 more)

### Community 32 - "Dhan Token Mint & Cooldown"
Cohesion: 0.08
Nodes (20): DhanMintRateLimitedError, jwt_exp(), Client, Exception, Path, Protocol, Response, Dhan refused to mint because the per-account cooldown is still running.… (+12 more)

### Community 33 - "Dhan Token Manager Lifecycle"
Cohesion: 0.09
Nodes (23): DhanTokenManager, Provides a valid Dhan access token, refreshing via TOTP before expiry., Return a currently-valid access token, refreshing if needed., Drop the ACTIVE token so the next ``token()`` refreshes. Called by API clients…, Persist a freshly-minted token for other processes. Writes BOTH peer sources:…, Publish a freshly-minted token to the remote store. Fail-soft. A remote outage…, _FakeHttp, A token whose exp is unparseable must not trigger endless refresh. (+15 more)

### Community 34 - "Strategy Stop Distance & Carry"
Cohesion: 0.09
Nodes (30): carry_interest(), Financing cost of a broker-FUNDED position, for ``days`` calendar days. Dhan…, expected_trigger_at_distance(), Trigger an absolute ``distance`` (quote currency) away from entry. The percent…, _entry_extra(), Facts stamped on the entry Trade for downstream stages to read back.…, _plan(), _pos() (+22 more)

### Community 35 - "EOD Digest Rendering"
Cohesion: 0.11
Nodes (30): The 10-line Telegram version. Readable on a phone, no tables., render_digest(), Quiet day" is a claim about the bot working. Don't make it unevidenced., Scanners running and finding nothing IS a quiet day, not a busy one., The whole point: proof the bot looked, on the day it did nothing. Without this,…, A pass count alone cannot tell "looked and found nothing" from "blind"., The exact digest that lied for two days. On 04/05 Aug 2026 a dead Dhan token…, Nothing ATTEMPTED is as blind as nothing evaluated, and can happen alone — a… (+22 more)

### Community 36 - "Scanner Bar Key Tests"
Cohesion: 0.11
Nodes (27): _bars(), capture(), _CapturingSession, _config(), datetime, fixture, parametrize, Scanner rows are keyed by BAR, not just by day. Both scanner tables are written… (+19 more)

### Community 37 - "Gaussian HMM Wrapper"
Cohesion: 0.09
Nodes (16): GaussianHMM, RuntimeError, _import_gaussian_hmm(), Any, DataFrame, ndarray, Deterministic seed list of length ``n_restarts``. Uses the fixed pool first; if…, Fit the HMM on a feature DataFrame and resolve state→label map. With… (+8 more)

### Community 38 - "Technical Indicators & Parity"
Cohesion: 0.09
Nodes (27): _engine_atr(), load(), main(), DataFrame, datetime, Parity check: does this repo's port reproduce the frozen 1h mean-rev backtest?…, The (date, bin) whose NEXT bin opens at ``entry_ist``. The backtest fills at…, The backtest's ATR read: calc_atr on the daily series, prior close. (+19 more)

### Community 39 - "Google Drive Archive Export"
Cohesion: 0.12
Nodes (25): _check(), main(), _oldest_audit_day(), date, Archive the audit-log backlog to Drive, oldest day first, and set the…, Report configuration without uploading anything., One-time Google Drive authorisation — mints the refresh token the archive uses.…, audit_archived_through() (+17 more)

### Community 40 - "Realized P&L Math"
Cohesion: 0.11
Nodes (18): bucket_ledger_pnl(), pnl_pct(), Decimal, Pure P&L math — no DB, no broker calls (importable by the backtester). Used by:…, P&L as % of traded notional. None when notional is not positive., Split realized round-trip P&Ls into (gross profit, gross loss). Profit = Σ…, Cumulative bot P&L for a bucket that does NOT own its wallet.…, Traded amount in quote currency for one order. (+10 more)

### Community 41 - "Stop Protection Planning Tests"
Cohesion: 0.17
Nodes (28): plan_stop_protection(), Diff exchange positions against resting protective stops. Args: positions: live…, _eqpos(), _pos(), Protective stop-loss planner (Decision 022) — pure logic, no I/O., A position the bot doesn't own gets NO stop (the 2026-07-22 bug)., The user's own resting stop on an unowned symbol must be left alone., Bot holds 100, exchange shows 150 (user also long 50) → stop only 100. (+20 more)

### Community 42 - "Bucket Runner Margin Fitting"
Cohesion: 0.11
Nodes (24): StrEnum, TradingType, _LevFeed, _NoPreflightBroker, _PricedBroker, Decimal, Feed that also answers the scrip-master leverage lookup., Broker whose venue offers no margin preflight. (+16 more)

### Community 43 - "Candlestick Pattern Flags"
Cohesion: 0.12
Nodes (27): pattern_flags(), DataFrame, Per-bar ``engulfing_bull`` / ``hammer`` booleans for an OHLC frame. ``df``…, GapDownReversal, Fade a 3–12% NIFTY-100 gap-down on the first bullish 5m reversal candle., _bar(), _Feed, _flat_session() (+19 more)

### Community 44 - "Bot-Owned Position Scoping"
Cohesion: 0.11
Nodes (26): bot_owned_quantities(), datetime, Decimal, Session, Bot position ownership on SHARED broker accounts (Decision 027 → 030-followup).…, ``{symbol: net_long_qty}`` the bot holds long — the DB-backed wrapper. Thin: it…, audit_violation(), check_scan_coverage() (+18 more)

### Community 45 - "EOD Edge & Slippage Payload"
Cohesion: 0.10
Nodes (27): build_edge(), _opt_str(), payload_of(), The structured numbers behind the prose, for later analysis., Live edge from closed round-trips. PURE. ``round_trips`` is [(realized_pnl,…, _edge(), EOD session report — pure builders and renderers, no DB., Both buckets went live in July 2026 — every early report hits this. (+19 more)

### Community 46 - "Regime Model Verification"
Cohesion: 0.11
Nodes (19): _json_safe(), Any, Recursively replace non-finite floats (NaN/Inf) with None. Verification and…, DataFrame, RegimeModel, Post-fit model verification (Markov 2.0 — FIX 2, label verification). A regime…, Validate a fitted ``model`` against the ``features`` it was trained on. Returns…, verify_model() (+11 more)

### Community 47 - "NSE Market Calendar"
Cohesion: 0.14
Nodes (24): NSE session state for this bucket's market. Crypto is 24/7, so it is always in…, is_trading_day(), nse_session(), NseSession, parse_ist_time(), date, datetime, StrEnum (+16 more)

### Community 48 - "EOD Report Sections"
Cohesion: 0.11
Nodes (26): build_report(), build_sections(), Position, Trade, Realized P&L the reconciler stamped on an exit, if any., Group the day's rows into one section per bucket. PURE. A bucket appears if it…, _realized_of(), to_carried() (+18 more)

### Community 49 - "Notional-to-Contracts Sizing"
Cohesion: 0.16
Nodes (15): AllocatorConfig, notional_inr_to_contracts(), Decimal, Convert an INR-denominated target notional into a whole-contract count. Pure…, Compute per-symbol sizing for one strategy in one bucket. Args: bucket: the…, size_positions(), _cfg(), Tests for ``notional_inr_to_contracts``. Pure function — no DB or broker… (+7 more)

### Community 50 - "Strategy Exit Selection"
Cohesion: 0.14
Nodes (12): Position, Phase 1: long-only entries on every symbol the scanner picked. Exit rule…, Top5VolumeLongterm, _bars_from_closes(), FakeMarketData, _FakePosition, _load(), Path (+4 more)

### Community 51 - "Broker Interface ABC"
Cohesion: 0.12
Nodes (19): main(), FillInfo, OrderResult, OrderType, ABC, datetime, StrEnum, Broker interface — the contract between exchange adapters and the order manager… (+11 more)

### Community 52 - "Equity Circuit Breakers"
Cohesion: 0.17
Nodes (22): BreakerResult, check_daily_drawdown(), check_funding_extreme(), check_liquidation_distance(), Any, Decimal, Circuit breakers — automatic safety checks that trip the kill switch. Each…, Trip if any held symbol has abs(funding rate) above threshold. ``data_source``… (+14 more)

### Community 53 - "Notional & Reject-Rate Invariants"
Cohesion: 0.13
Nodes (24): check_notional_ceiling(), check_reject_rate(), check_stop_coverage(), Decimal, Every bot-held position needs a resting reduce-only stop (Decision 022). Runs…, Committed notional must stay inside the bucket's capital × leverage. Catches a…, Repeated broker rejections mean the order path is broken, not unlucky., Session invariants — pure logic, no I/O (mirrors test_stop_protection.py). (+16 more)

### Community 54 - "EMA 9/15 Crossover Strategy"
Cohesion: 0.15
Nodes (11): Ema9_15Crossover, Long when EMA(9) just crossed above EMA(15) on the most recent bar., _bars_from_closes(), _close_series_cross_up(), _close_series_flat(), FakeMarketData, Unit tests for the EMA 9/15 crossover strategy. Strategy under test:…, If fast was already above slow on the previous bar, no entry. (+3 more)

### Community 55 - "Square-Off & Liveness Invariants"
Cohesion: 0.13
Nodes (23): check_bucket_liveness(), check_squareoff(), An intraday bucket must be flat once its square-off deadline passes. Fires from…, This bucket completed a pipeline pass within a few of its own cadences. The…, _ist(), An un-squared MIS position at 16:00 is worse, not stale., swing-indian holds MTF for days — 15:15 means nothing to it., NOTICE, never HALT: a bucket that isn't running already isn't entering. (+15 more)

### Community 56 - "Equity Scanner Daily Pass"
Cohesion: 0.18
Nodes (22): load_scanner_config(), Path, daily_pass(), Apply the prior-close daily filters. Returns a Survivor or None. Gap and volume…, test_scanner_config_uses_intraday_engine_and_full_universe(), _cfg(), _daily(), _intraday() (+14 more)

### Community 57 - "Delta Client Retry Hardening"
Cohesion: 0.20
Nodes (17): _client(), FakeHttp, FakeResponse, Any, MonkeyPatch, Delta client hardening: retries, 429, clock skew, catalogue TTL., Delta signs METHOD+TS+PATH+'?'+QUERY+BODY — the '?' is mandatory when params…, Yields scripted responses; raising entries are raised instead. (+9 more)

### Community 58 - "Dhan Data Fetch Tests"
Cohesion: 0.20
Nodes (18): _data(), _FakeHttp, Dhan market-data adapter — OHLCV + quote parsing (Phase 3/4)., Fail by name rather than emit a 401 the caller will misread as an expired token…, The retry path had its own copy of the headers; both were empty., Routes POSTs by URL suffix to queued responses; records calls., _Resp, test_401_triggers_token_invalidate_and_retry() (+10 more)

### Community 59 - "Settings & Binance Data"
Cohesion: 0.10
Nodes (9): BaseSettings, SecretStr, Is there a destination AND some way to authenticate to it? Both halves matter…, Application settings — read from environment, validated at startup., Settings, BinanceWS, Async WebSocket client for Binance Futures public streams. Usage:: ws =…, Connect to Binance combined stream. *streams*: ``["btcusdt@ticker",… (+1 more)

### Community 60 - "Broker Account Config"
Cohesion: 0.13
Nodes (18): NamedTuple, DeltaAccount, DhanAccount, LogFormat, StrEnum, Centralised configuration loaded from environment variables. House rules…, Resolve credentials for a Delta India (sub-)account in the active mode.…, Resolve Dhan config for the active mode (fail-fast, House Rule #6). Requires… (+10 more)

### Community 61 - "Archive Lag Watchdog"
Cohesion: 0.12
Nodes (21): archive_lag_days(), How many days behind ``today`` the archive is. PURE. ``None`` means nothing has…, parametrize, Archive dead-man's switch — the watermark stops moving, someone gets paged.…, None is its own alarm: "never set up" reads differently from "stopped"., The job archives YESTERDAY, so lag 1 is correct, not late., The message quotes the lag, so it must be the real number of days., A manual backfill can put it at today; that must not read as negative. (+13 more)

### Community 62 - "Regime Feature Computation"
Cohesion: 0.13
Nodes (12): compute_features(), DataFrame, Feature pipeline for the regime HMM. Pure functions: input a price DataFrame…, Compute features from OHLCV bars. Args: bars: DataFrame with columns at least…, label_states_by_mean_return(), ndarray, State→label mapping for the HMM. After fitting, the model's hidden states are…, Map state index → MarketRegime by sorting on mean log return. Args: means:… (+4 more)

### Community 63 - "HMM Fit & Predict Round-Trip"
Cohesion: 0.16
Nodes (12): HMM wrapper. A thin shell around ``hmmlearn.GaussianHMM`` that handles: -…, Continuous regime conviction in [-1, 1]: ``P(bull) − P(bear)``. Sign =…, Fit + predict + serialise a Gaussian HMM. ``n_states`` and ``covariance_type``…, RegimeModel, RegimePrediction, DataFrame, End-to-end: fit a small HMM and confirm it round-trips through to_dict /…, Mixed bull/neutral/bear blocks to give the HMM something to find. (+4 more)

### Community 64 - "Blind Scanner Coverage Check"
Cohesion: 0.13
Nodes (22): _coverage(), _from_payload(), datetime, The outage itself: 94 symbols attempted, 0 evaluated, for two days., A bad scrip master empties the F&O filter before any fetch is tried., It evaluates nothing, so it enters nothing — halting prevents nothing already…, Nearly-all-unusable has honest causes (a stub bin after a restart), so it must…, Silence is bucket_liveness's job. Double-reporting one fault as two invariants… (+14 more)

### Community 65 - "Phase 4 Stocks Swing Milestones"
Cohesion: 0.17
Nodes (21): Bar-selection bugfix (locate_bin / _through_bin, 2026-08-01), _blasting_momentum.py (made inert), MTF carry interest on (notional - margin) at 14.6%/yr, Circuit filter - 'F&O underlying OR hard band >=20%', Dhan sandbox edge blocks datacenter IPs, gap_down_reversal.py Strategy (first reversal candle, 15:15 square-off), scripts/gap_reversal_parity.py (75/76), src/shared/scanner/gap_reversal.py + engine equity_intraday (+13 more)

### Community 66 - "Net Owned Quantity"
Cohesion: 0.23
Nodes (20): net_owned(), ``{symbol: net_long_qty}`` from a set of the bot's own trades. PURE. BUY…, _buy(), Bot ownership on shared accounts (Decision 027 followup). The rule that keeps…, No bot trade for a symbol → never owned (the 2026-07-22 bug)., A just-placed entry counts, so the first reconcile recognises it., A not-yet-filled exit must not make the bot abandon a held position., _sell() (+12 more)

### Community 67 - "Kelly Fraction Math"
Cohesion: 0.18
Nodes (9): fractional_kelly(), kelly_fraction(), Decimal, Kelly criterion — pure math. For continuous returns (the right form for…, Return f* = μ / σ², clamped at 0 for non-positive edge. Args: mu: expected per-…, Scale Kelly by a fixed fraction (default 0.25). Pure., Unit tests for shared.allocator.kelly — pure math., TestFractionalKelly (+1 more)

### Community 68 - "CI Deploy Gate & Bucket Registry"
Cohesion: 0.14
Nodes (18): CI workflow (deploy gate Layer 1), CI Lint step (ruff check src tests), CI minimal test env (dummy creds, sqlite, testnet), Python 3.11 for prod parity with the VM, CI Unit tests step (pytest tests/unit), Top-level bucket registry (buckets.yaml), gambling-crypto bucket, longterm-crypto bucket (+10 more)

### Community 69 - "Blind Scan Outage & EOD Tier 3"
Cohesion: 0.13
Nodes (18): backtest_baseline in allocator.yaml (live-vs-backtest edge), Two-day blind scan outage (2026-08-04/05), Layer-1 deploy gate (CI green required, pre-restart selfcheck), Every deploy restarts the bot and is a real risk event, Dhan 5 req/s cap - 429 backoff + 0.22s pacing, Shared on-disk / Postgres Dhan token cache, Dhan single-session token eviction and mint cooldown, Empty runner set exited 0, so systemd never restarted (80-min outage) (+10 more)

### Community 70 - "Alert Dedup & Recovery"
Cohesion: 0.28
Nodes (17): note_sustained_failure(), Record a failure for ``key``; page only once it has lasted ``grace_seconds``.…, Clear one or all dedup + sustained counters. Test helper / success hook., reset_alert_dedup(), _capture(), _fake_clock(), Tests for the time-windowed alert dedup (``src.core.alerts``). Covers the…, Replace ``send_alert`` with a recorder; return its message list. (+9 more)

### Community 71 - "Dhan Rate Limit Retry"
Cohesion: 0.20
Nodes (14): MockTransport, Response, Parse a ``Retry-After`` header (delta-seconds form) into seconds., _retry_after_seconds(), _client(), MonkeyPatch, Dhan charts fetch survives the 5 req/s Data-API cap. Regression for 2026-07-22:…, _StubToken (+6 more)

### Community 72 - "Live Mode Credential Validation"
Cohesion: 0.18
Nodes (15): model_validator, _enabled_bucket_brokers(), Ensure the broker keys exist for the active mode. Scoped to brokers an ENABLED…, Broker names used by currently ENABLED buckets, from ``buckets.yaml``. Read as…, MonkeyPatch, Live-mode credential validation is scoped to ENABLED buckets. Regression for…, Hermetic Settings — no repo .env, no ambient DELTA_* env vars. Both matter: the…, A valid rollout stage: everything paused, nothing to authenticate. (+7 more)

### Community 73 - "EOD Markdown Formatting"
Cohesion: 0.14
Nodes (13): _edge_block(), _money(), _pct(), Decimal, _ratio(), Live edge vs the backtest that justified running the strategy. The point of the…, The full journal entry — one file per trading date., render_markdown() (+5 more)

### Community 74 - "Allocation Caps"
Cohesion: 0.19
Nodes (9): apply_aggregate_cap(), apply_per_symbol_cap(), Decimal, Position-weight caps — pure functions. A "weight" here means "fraction of…, Clamp every weight at ``cap``. Pure. Args: weights: raw {symbol: weight}. cap:…, Scale weights down proportionally if their sum exceeds ``cap``. Pure. Args:…, Unit tests for shared.allocator.caps — pure math., TestAggregateCap (+1 more)

### Community 75 - "Phase Build Tracker"
Cohesion: 0.16
Nodes (16): BucketRunner pipeline orchestrator, buckets.yaml (per-bucket capital, leverage, stops, cadence), PHASES.md Build Tracker, TF-scaled dedup window (dedup_window_hours_for_tf), ema_9_15.py Strategy (swing-crypto), Phase 1 - Crypto Long-term, Phase 1a - Bucket framework (Decisions 013-017), Phase 1b - Soak restart on new structure (+8 more)

### Community 76 - "Delta WebSocket Client"
Cohesion: 0.18
Nodes (8): DeltaIndiaWS, Any, Delta Exchange India WebSocket client. Handles authentication (``key-auth``),…, Register a callback for messages whose ``type`` matches *channel*., Receive messages and dispatch to callbacks. Blocks until :meth:`close` is…, Async WebSocket client for Delta Exchange India., Connect and authenticate. Re-subscribes if reconnecting., Subscribe to one or more channels. Channels are remembered so they can be re-…

### Community 77 - "Binance Market Data"
Cohesion: 0.16
Nodes (6): BinanceData, Any, Return ``[{symbol, baseAsset, quoteAsset}, ...]`` for active perps., Register a callback for a specific stream name., Synchronous REST client for Binance Futures public data., Return raw symbol entries from ``/fapi/v1/exchangeInfo``.

### Community 78 - "Blasting Momentum (inert)"
Cohesion: 0.28
Nodes (12): BlastingMomentum, Buy the scanner's ranked gap-momentum basket; exit on daily ST flip / 30d., _daily(), _FakeData, _Pos, datetime, Blasting Momentum strategy — entries + Supertrend/max-hold exits (Phase 4). The…, Minimal stand-in for a Position row (strategy only reads opened_at). (+4 more)

### Community 79 - "Dashboard App & CSRF"
Cohesion: 0.21
Nodes (13): get_settings(), Cached settings accessor — single load per process. Lazy on purpose: importing…, create_app(), _num3(), Render Decimal/float/int with 3 decimal places. ``None`` → em-dash., main(), Launch the dashboard with uvicorn., _auth_headers() (+5 more)

### Community 80 - "Foreign Position Invariant"
Cohesion: 0.18
Nodes (15): check_foreign_positions(), effective_holdings(), ``{symbol: qty}`` the bot actually holds RIGHT NOW on the exchange. PURE. The…, Report account positions the bot did not open. NOTICE — never acted on. On…, _pos(), The 2026-07-22 near-miss: the user's NIFTY options on the bot's account., Dhan's own MIS auto-square-off closes without writing our SELL row. Ownership…, test_foreign_positions_flags_a_partially_owned_symbol() (+7 more)

### Community 81 - "Bucket Config Loader"
Cohesion: 0.13
Nodes (15): load_bucket(), Convenience: load all and return the one matching ``bucket_id``. Raises…, Broad set taken live (user decision 2026-07-27): both sets now trade. The…, NIFTY-100 is ~all F&O, so a band filter there would be noise., 20% cap × 5 = 100% of capital; the two sets share those 5 slots., A bucket without ``fallback_product`` never opts into the retry., MTF→CNC must be size-capped, not a full-notional cash order. Without…, test_both_sets_active_and_universes_disjoint() (+7 more)

### Community 82 - "Stop Attribution Merge"
Cohesion: 0.18
Nodes (8): The exact failure: a fill with no Position row yet., Position reflects the exchange; a Trade only reflects what we asked., Trades arrive newest-first, same convention as _load_stop_distances., An exit would name the strategy that CLOSED the position, not the one holding…, plan_stop_protection reads .get(sym, (None, None)); absence must stay absence…, Documents the REAL behaviour, which is the opposite of what an earlier version…, _t(), TestMergeAttribution

### Community 83 - "Locked Decisions Table"
Cohesion: 0.19
Nodes (14): account_ref → DELTA_<REF>_<MODE>_API_KEY sub-account mapping, Quarter-scale capital_inr sizing (never raise per_symbol_cap), One Delta India sub-account per crypto bucket — Decision 019, Fixed USD/INR rate 85 in allocator.yaml, Kelly sizing on live sub-account equity — Decisions 025/027, Locked Decisions table, Exchange-resident protective stops — Decision 022/032, Per-bucket 3-state HMM regime brain — Decision 014 (+6 more)

### Community 84 - "Journal Markdown-to-HTML"
Cohesion: 0.14
Nodes (14): _inline(), journal_index(), get, Request, Newest report by default; ``?d=YYYY-MM-DD`` picks a specific session., Escape first, then apply the inline subset — never the other way round., Render the closed subset ``eod.render_markdown`` emits. PURE. Everything is…, render_markdown_to_html() (+6 more)

### Community 85 - "Overnight Position Split"
Cohesion: 0.18
Nodes (13): Split open positions into (the bot's, everyone else's). PURE. The 2026-07-22…, split_positions(), _Position, Live rows 244/245: orphan-imported before the scoping fix. No bucket_id, so…, Crypto sub-accounts are exclusively the bot's (Decision 019). ``owned`` is…, test_an_attributed_and_owned_position_is_the_bots(), test_an_attributed_position_absent_from_the_ledger_is_not_the_bots(), test_an_unattributed_position_is_not_the_bots() (+5 more)

### Community 86 - "Broad Gap Reversal Rescreen"
Cohesion: 0.14
Nodes (8): GapDownReversalBroad, Gap-down reversal over the BROAD universe (Midcap 150 + Smallcap 100).…, Same fade, broader (unvalidated) universe., A pass where every symbol was data-limited answered no question., An empty cut with real rejections is a genuine answer about the market, not a…, 230 symbols x 2 calls takes 113s against a 60s tick and a 5 req/s cap shared…, Never screened today — the caller's normal first-pass path., TestShouldRescreen

### Community 87 - "Stop Place Retry Budget"
Cohesion: 0.20
Nodes (4): A stop the venue refuses is refused deterministically. Retrying it every 90s…, The band clamp or a late-arriving strategy distance changes the price — that is…, The position is still uncovered and the invariant must keep saying so — the…, TestPlaceRetryBudget

### Community 88 - "Scheduler Nightly Jobs"
Cohesion: 0.22
Nodes (12): prune_old_rows(), Delete expired snapshot/audit rows. Returns per-table delete counts. Each table…, _eod_report(), _heartbeat_watch(), main(), _nightly_export(), _nightly_prune(), APScheduler-based background job runner (Railway service). Jobs: - Nightly… (+4 more)

### Community 89 - "swing-indian Live Bucket"
Cohesion: 0.26
Nodes (12): carry_interest_apr (MTF funding subtracted from realized P&L), entry_start / entry_end coarse entry window, fallback_product CNC (1x degrade instead of skipping), intraday-indian bucket (NIFTY-100 gap-down reversal) — Decision 029, Operational: do not log into Dhan while the bot runs, stop_loss_pct (0.5/leverage broker-side stop distance), swing-indian bucket (LIVE, Dhan MTF), tick_interval_seconds (1h strategy under a 1d regime model) (+4 more)

### Community 90 - "Regime Persistence Diagnostic"
Cohesion: 0.20
Nodes (10): _diag_pstay(), persistence_diagnostic(), PersistenceDiagnostic, Any, DataFrame, ndarray, RegimeModel, Persistence / autocorrelation diagnostic (Markov 2.0 — FIX 1, adapted). The… (+2 more)

### Community 91 - "House Rules"
Cohesion: 0.18
Nodes (11): bucket_state table seeded by migration 0002, mirrors wallet, Audit log every decision, House Rules (non-negotiable), Idempotent orders / deterministic client_order_id, Postgres is the source of truth, Same code path for backtest and live, Secrets only via Railway env vars, redacted in logs, Strategy parameters live in YAML in git with backtest_ref (+3 more)

### Community 92 - "Phase 0/1 Foundations"
Cohesion: 0.27
Nodes (11): Authority ladder - supervision may HALT, only breakers may FLATTEN, Equity breakers (daily DD, liquidation distance, funding extreme), Daily-anchored drawdown breaker (Decision 023), Delta client hardening (central _request), Breaker enforcement (trip -> kill switch + flatten), Fills / fees / realized-P&L ingestion, Kill switch (DB row, blocks risk-increasing actions only), Phase 0 - Foundations (+3 more)

### Community 93 - "Regime Model Store"
Cohesion: 0.24
Nodes (10): RegimeModelRow, load_latest_for_symbol(), Any, datetime, RegimeModel, Session, Persist and retrieve fitted HMM artifacts via Postgres JSONB. ``regime_model``…, Insert a new fitted-model row. Caller commits. (+2 more)

### Community 94 - "Journal Export Watermark"
Cohesion: 0.18
Nodes (11): export(), main(), date, Path, Write reports to docs/journal/. Returns the paths actually written., fixture, In-memory stand-in for the heartbeat row., No archive → the audit table is reported BLOCKED and left alone. (+3 more)

### Community 95 - "Signal Delivery Invariant"
Cohesion: 0.18
Nodes (11): check_signal_delivery(), A signal the strategy DID produce reached the broker, or you hear why.…, _delivery(), A healthy scan found BLUESTARCO; the quote endpoint 401'd; the sizer had no…, The trade is already lost; halting only stops the NEXT one., 38 retries of the same signal is one problem, not 38., test_signal_delivery_dedups_repeated_misses_of_one_symbol(), test_signal_delivery_never_halts() (+3 more)

### Community 96 - "Tick Cadence by Timeframe"
Cohesion: 0.27
Nodes (10): Seconds between full pipeline passes for a bucket at timeframe ``tf``. A 1d…, tick_interval_for_tf(), Without an override, the bucket paces to its FASTEST tf, not the regime's., test_cadence_falls_back_to_the_fastest_timeframe(), Per-bucket tick cadence from bucket timeframe (Phase 1c)., test_case_insensitive_unit(), test_fast_tfs_stay_at_60s(), test_garbage_falls_back_to_60s() (+2 more)

### Community 97 - "Stop Clamp Tightening Only"
Cohesion: 0.18
Nodes (11): _piind_plan(), Documents what actually shipped: -20%, which PIIND's 10% circuit band makes…, A tighter stop that EXISTS beats a correctly-sized one that does not., The intended stop was always inside the band — the clamp is only a net for when…, It must never widen a stop past what was asked for., Crypto perps have no circuit band; behaviour must be unchanged there., test_band_clamp_makes_the_fallback_placeable(), test_clamp_only_ever_tightens() (+3 more)

### Community 98 - "Project Bible & North Star"
Cohesion: 0.24
Nodes (10): Phase build order (Phase 0 → 8), No LLM in the trading decision loop, North Star / Goal_Setting.txt bible, Project Bible Pointer (CLAUDE.md), Repo layout orientation map, Session Resume Protocol, Optional agentic perimeter (later phases), Deterministic core (+2 more)

### Community 99 - "Kill Switch & Runbook"
Cohesion: 0.20
Nodes (10): Kill switch is a DB row, Kill-switch blocks only risk-increasing actions — Decision 024, Google Drive archive (nightly trades + audit export), Kill switch operations (DB row, /kill-switch page), Operations Runbook, Service accounts cannot own files on a personal Drive, Google Drive client deps (added after the missing-dep fault), requirements.txt runtime manifest (VM install source) (+2 more)

### Community 100 - "Broker Sizing Primitives"
Cohesion: 0.20
Nodes (5): Decimal, Margin the venue will actually demand for this order, or None. Exists because a…, Units of base asset per contract, from the venue's product spec. Returns…, Price increment from the venue's product spec; None when unknown. Used to snap…, (total_deposited, total_withdrawn) in the account's settlement currency, summed…

### Community 101 - "Clock Abstraction"
Cohesion: 0.24
Nodes (4): FakeClock, datetime, Return the current UTC datetime (timezone-aware)., Test/replay clock. Advance manually with ``tick()`` or ``set()``.

### Community 102 - "Regime Window Anchoring"
Cohesion: 0.29
Nodes (5): datetime, Truncate ``now`` to the start of the current bar for ANY ``<N><unit>`` TF.…, _window_start(), Bar-window truncation must work for ANY <N><unit> TF (Phase 1c — 4h/15m buckets…, TestWindowStart

### Community 103 - "8-Step Bucket Pipeline"
Cohesion: 0.25
Nodes (9): Bucket architecture (type x market, isolated capital) — Decision 013, 8-step BucketRunner pipeline per bucket, dhan-prepare.timer / scanner prepare_job → ScannerSnapshot, Swing-Indian bucket operations (Dhan sandbox, Phase 4), Interim Dhan Scanner (Phase-4 bridge tool), scanner_live.py manage (15:15 Supertrend-flip / 30-day exits), Tool sits OUTSIDE the deterministic bot loop, scanner_live.py prepare (18:00 IST ~4,600-symbol pass) (+1 more)

### Community 104 - "Heartbeat & Archive Watermark"
Cohesion: 0.25
Nodes (9): Archive dead-man's switch on the VM tick loop, Contiguous archive watermark guard, Google Drive audit_log archive + OAuth, Heartbeat / dead-man's switch, bucket_liveness invariant (NOTICE, per-bucket heartbeat), Phase 7a - Live-session supervision (Decision 033), Nightly retention prune (snapshots 60d, audit_log 180d), Tier 1 - Deterministic session invariants (+1 more)

### Community 105 - "Heartbeat Staleness"
Cohesion: 0.36
Nodes (8): Pure staleness check → (is_stale, age_seconds). A missing heartbeat…, staleness(), Heartbeat staleness math (dead-man's switch) — pure, no DB., test_exactly_threshold_is_stale(), test_fresh_beat_is_not_stale(), test_future_beat_is_not_stale(), test_missing_beat_is_stale_with_no_age(), test_old_beat_is_stale()

### Community 106 - "Dashboard Basic Auth"
Cohesion: 0.42
Nodes (3): _check_basic_auth(), Constant-time check of an ``Authorization: Basic ...`` header., TestBasicAuth

### Community 107 - "Fill Aggregation"
Cohesion: 0.31
Nodes (5): aggregate_fills(), FillAggregate, Volume-weighted summary of all fills for one exchange order., Aggregate (price, size, commission) tuples into one summary. Returns None when…, TestAggregateFills

### Community 108 - "Bucket Cumulative P&L"
Cohesion: 0.31
Nodes (5): bucket_cumulative_pnl(), Cumulative bot P&L for a bucket whose wallet mirrors a sub-account. equity =…, Two Indian buckets on one Dhan account must not report each other's P&L. The…, test_ledger_pnl_is_independent_of_the_shared_wallet(), TestBucketCumulativePnl

### Community 109 - "Dedup Window by Timeframe"
Cohesion: 0.36
Nodes (8): dedup_window_hours_for_tf(), Dedup window ≈ one strategy bar, with a 1/24 early-rebalance buffer. 23/24 of…, TF-scaled dedup window (Phase 1c — replaces hardcoded 23h)., test_daily_tf_matches_legacy_23h(), test_five_minute_tf(), test_four_hour_tf(), test_garbage_falls_back_to_23h(), test_hourly_tf()

### Community 110 - "Sizing Equity Source"
Cohesion: 0.36
Nodes (8): The equity Kelly sizes against, per market (pure math). Crypto (Decision 025):…, sizing_equity(), Decision 027 — per-market sizing equity (Indian allocation cap). Dhan has no…, test_crypto_uses_wallet_untouched(), test_indian_caps_at_allocation(), test_indian_negative_adjustment_lowers_cap(), test_indian_positive_adjustment_raises_cap(), test_indian_wallet_below_allocation_floors_at_wallet()

### Community 111 - "Entrypoint Import Smoke Tests"
Cohesion: 0.22
Nodes (3): Smoke tests: every service entrypoint must be importable. These tests caught…, Importing the brain entrypoints must not require hmmlearn at import time.…, test_regime_modules_import_without_hmmlearn()

### Community 112 - "Reconciler Bucket Scoping"
Cohesion: 0.33
Nodes (8): Reconciler bucket-id scoping (Decision 019). With one sub-account per bucket,…, Crypto (the default) must keep treating the whole account as the bot's., _reconciler(), test_bucket_ids_produce_one_clause_each(), test_clause_targets_bucket_id_column(), test_no_bucket_ids_means_no_extra_clauses(), test_shared_account_defaults_false(), test_shared_account_flag_stored()

### Community 113 - "Square-Off Invariant"
Cohesion: 0.36
Nodes (8): foreign_positions invariant (NOTICE, never acted on), squareoff invariant (HALT), stop_coverage invariant (HALT, 2-tick sustain), Dhan MTF consent checkbox blocker (2026-08-11), net_owned() bot-ownership scoping on shared accounts, src/safety/session_invariants.py - six process checks, Stop attribution / product / self-recognition fix (2026-08-12), Exchange-resident protective stop sweep (Decision 022)

### Community 114 - "Profit Factor"
Cohesion: 0.25
Nodes (8): profit_factor(), Gross profit ÷ gross loss — the backtest's headline number. Scale-invariant,…, Printing inf beside a backtest's 2.31 would read as spectacular., Which is what makes live rupees comparable to a backtest's returns., test_profit_factor_is_gross_profit_over_gross_loss(), test_profit_factor_is_scale_invariant(), test_profit_factor_of_nothing_is_undefined(), test_profit_factor_with_no_losers_is_undefined_not_infinite()

### Community 115 - "Dhan Sandbox vs Live Config"
Cohesion: 0.43
Nodes (7): Dhan config resolution — Settings.dhan_account() (Phase 3/4)., _settings(), test_live_orders_reuse_live_data_token(), test_missing_data_auth_raises(), test_static_data_token_satisfies_data_auth(), test_testnet_missing_sandbox_creds_raises(), test_testnet_orders_go_to_sandbox()

### Community 116 - "Exchange Side Mapping"
Cohesion: 0.48
Nodes (4): _exchange_side_to_position(), Map the broker's free-form side string to our enum. Delta India and most perp…, parametrize, TestExchangeSideToPosition

### Community 117 - "IST Day Bounds"
Cohesion: 0.29
Nodes (7): ist_day_bounds(), date_, datetime, ``{bucket_id: [(realized_pnl, exit_notional)]}`` for closed round-trips. A…, UTC-comparable [start, end) for one IST calendar day., round_trips_by_bucket(), test_day_bounds_span_one_ist_day()

### Community 118 - "Strategy Entry Interface"
Cohesion: 0.29
Nodes (3): EntryCandidate, One symbol a strategy wants to enter. ``side`` is "buy" or "sell" — the order…, Return entry candidates from the scanner's filtered list. Pure function of…

### Community 119 - "Dhan Stop Order Recognition"
Cohesion: 0.43
Nodes (3): plan_stop_protection only matches an existing stop when reduce_only is True.…, No correlationId ⇒ not ours ⇒ plan_stop_protection must not cancel it. This is…, TestDhanStopRecognition

### Community 120 - "Signal Delivery Notice"
Cohesion: 0.40
Nodes (6): data_ failure vs allocator decision - reasons must be distinguishable, client-id: "" on /v2/marketfeed/quote - the real cause of zero fills, signal_delivery invariant (NOTICE), Kelly allocator / sizer with skip rules, Per-scrip leverage via /v2/margincalculator, never predicted, Multiple named scanner sets per bucket (Decision 026)

### Community 121 - "Dhan Symbol Resolution"
Cohesion: 0.33
Nodes (3): ResolveSymbol, Client, Build a client for the active mode (House Rule #6). In testnet the order token…

### Community 123 - "Migration 0002 Buckets"
Cohesion: 0.50
Nodes (3): _add_enum_value_if_missing(), Idempotent ALTER TYPE ... ADD VALUE for Postgres., upgrade()

### Community 124 - "Stop Trigger Price"
Cohesion: 0.40
Nodes (5): expected_trigger(), Trigger price ``stop_pct`` percent away from entry, snapped to tick. Longs stop…, test_long_trigger_below_entry(), test_short_trigger_above_entry(), test_trigger_snaps_to_tick()

### Community 125 - "Pattern Series Helpers"
Cohesion: 0.40
Nodes (4): _bshift(), Series, Candlestick patterns — ported byte-for-byte from the Backtesting Engine…, Shift a boolean Series without upcasting to object (missing → False).

### Community 128 - "Win Rate"
Cohesion: 0.50
Nodes (4): Share of round-trips that made money, 0..1. None on an empty list., win_rate(), test_win_rate_counts_only_strict_winners(), test_win_rate_of_nothing_is_undefined()

### Community 140 - "ORM Column Parity Test"
Cohesion: 0.67
Nodes (3): parametrize, Every attribute the fakes expose must exist on the real model. These tests…, test_fakes_match_the_real_orm_columns()

## Ambiguous Edges - Review These
- `Locked Decisions table` → `README claim: equities via Zerodha Kite (stale vs Decision 012)`  [AMBIGUOUS]
  README.md · relation: conceptually_related_to

## Knowledge Gaps
- **50 isolated node(s):** `setup-dhan.sh script`, `trading-bot`, `Repo layout orientation map`, `swing-crypto bucket`, `scalp-crypto bucket` (+45 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **17 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Locked Decisions table` and `README claim: equities via Zerodha Kite (stale vs Decision 012)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `session_scope()` connect `DB Session, Migrations & Ops Scripts` to `ORM Models & EOD Reporting`, `Bot Entrypoint, Alerts & Clock`, `Scanner Engine & Filters`, `Logging & Dry-Run Harnesses`, `Google Drive Archive Export`, `Tax Ledger & Trade Export`, `Dashboard Bucket Routes`, `Order Manager & Stop Protection`, `Bot-Owned Position Scoping`, `Delta Market Data & Symbol Sync`, `Notional-to-Contracts Sizing`, `Journal Markdown-to-HTML`, `Regime Retrain Job`, `Scheduler Nightly Jobs`, `Dhan Token Store (Postgres)`, `Journal Export Watermark`, `Position Reconciler`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Why does `DhanTokenManager` connect `Dhan Token Manager Lifecycle` to `Dhan Token Mint & Cooldown`, `Gap Reversal Parity & Data Source ABC`, `Dhan Broker Order Tests`, `Dhan Data Fetch Tests`, `Dhan Broker Client`, `Dhan Auth Test Fixtures`, `Transient vs Fatal Error Classification`, `Dhan Market Data Adapter`, `Dhan Symbol Resolution`, `Dhan HTTP Fakes`, `Dhan Token Store (Postgres)`?**
  _High betweenness centrality (0.050) - this node is a cross-community bridge._
- **Why does `OHLCVBar` connect `Gap Reversal Parity & Data Source ABC` to `Scanner Engine & Filters`, `Logging & Dry-Run Harnesses`, `Technical Indicators & Parity`, `Mean Reversion 1h Tests`, `Scanner Bar Key Tests`, `Candlestick Pattern Flags`, `Binance Market Data`, `Mean Reversion Scan Engine`, `Blasting Momentum (inert)`, `Delta Market Data & Symbol Sync`, `Strategy Exit Selection`, `Dhan Market Data Adapter`, `EMA 9/15 Crossover Strategy`, `Equity Scanner Daily Pass`, `Gap Reversal Screen Tests`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **Are the 37 inferred relationships involving `AuditLog` (e.g. with `Base` and `KillSwitchEngagedError`) actually correct?**
  _`AuditLog` has 37 INFERRED edges - model-reasoned connections that need verification._
- **Are the 68 inferred relationships involving `timedelta` (e.g. with `main()` and `cmd_prepare()`) actually correct?**
  _`timedelta` has 68 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `MarketRegime` (e.g. with `FakeMarketData` and `Base`) actually correct?**
  _`MarketRegime` has 32 INFERRED edges - model-reasoned connections that need verification._