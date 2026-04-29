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
