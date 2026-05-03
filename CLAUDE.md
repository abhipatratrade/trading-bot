# CLAUDE.md — Project Bible Pointer

> This file is auto-loaded by Claude Code on session start. Read it first.
> If anything below contradicts a chat instruction, **this file wins** unless
> the user explicitly says "amend DECISIONS.md".

---

## North Star

The user's long-term goals live in `C:\Users\User\Desktop\Goal_Setting.txt`.
That file is the **bible**. Treat it as immutable unless the user edits it.

This repo implements **Goal 1 (Portfolio Management)** plus the cross-cutting
infrastructure for Goals 2 (Scanner) and 4 (Portfolio Allocator).
**Goal 3 (Backtesting)** is built separately by the user — not in this repo.

---

## Resume Protocol

When the user opens a new session and says one of these:

| User says | You do |
|---|---|
| `continue` | Read `docs/PHASES.md`, find the next unchecked item, resume. |
| `start phase N` | Verify all earlier phases are fully checked. If not, list gaps. Otherwise begin phase N. |
| `status` | Summarise: completed phases, current phase, next 1-3 actions, any blockers. |
| `pause and document` | Update `docs/PHASES.md`, commit, stop. |

Always end a working session by ticking checkboxes in `docs/PHASES.md` and
committing. The next session relies on it.

---

## Locked Decisions (do not re-litigate in chat)

Full rationale lives in `docs/DECISIONS.md`. Quick reference:

| Decision | Value |
|---|---|
| Language | Python |
| Hosting | Railway (all services + Postgres) |
| Dashboard | FastAPI + HTMX (single Python service) |
| Crypto signals data | Binance public WS/REST |
| Crypto execution | Delta Exchange India (HMAC client) |
| Crypto funding source (for logic) | Always Delta India's funding rate |
| Stocks data + execution | Dhan (DhanHQ API) — see Decision 012 |
| Strategy params | YAML in git, schema-validated, audit-logged, `backtest_ref` required |
| Trade archive | Postgres (truth) + nightly Parquet/CSV → Google Drive auto-sync to local |
| Alerts channel | Telegram |
| Architecture | Deterministic core now; agentic perimeter (postmortem, research, news) later |
| Backtest engine | Out of scope for this repo |
| Options trading | Deferred until all futures/spot phases live |

---

## House Rules (non-negotiable)

1. **No LLM in the trading decision loop.** Agentic tools are advisory only,
   in a separate later phase. The core scanner→brain→allocation→safety→broker
   path must be fully deterministic.
2. **Idempotent orders.** Every order has a deterministic `client_order_id`.
   Network retries must never double-fire.
3. **Postgres is the source of truth.** Bot state survives restarts via DB,
   never in-memory only. On startup the reconciler compares DB vs exchange.
4. **Kill switch is a DB row.** Checked every loop. Editable from dashboard
   without redeploy. Never a code path that requires a deploy to stop trading.
5. **Secrets only via Railway env vars.** Never committed, never logged.
   Log filter must redact anything matching API key patterns.
6. **Testnet vs live is an env var, not a code branch with a default.**
   `TRADING_MODE` must be explicitly set; no implicit default to live.
7. **Strategy parameters live in YAML in git.** Not in DB, not hardcoded,
   not editable from the dashboard. Every change cites a `backtest_ref`.
8. **Audit log every decision.** Regime change, order placed, breaker tripped
   — all rows. This is the only forensic tool when something goes wrong.
9. **Same code path for backtest and live.** When backtester is built
   (separately), it imports `src/scanner`, `src/allocator`, `src/safety`
   directly. No reimplementation.
10. **Never skip pre-commit hooks** unless the user explicitly asks.

---

## Active Phase

See `docs/PHASES.md`. Current pointer: **Phase 0 — Foundations**.

---

## Build Order (high level — details in PHASES.md)

```
Phase 0  Foundations (this session is the start)
Phase 1  Crypto Long-term [priority 1]   1D, 5x, top-5 vol
Phase 2  Crypto Swing      [priority 2]   1H/4H, 10x, scanner
Phase 3  Stocks Long-term  [priority 3]   integrate existing Kite system
Phase 4  Stocks Swing      [priority 4]   15M/1H, 3-4x, scanner
Phase 5  Crypto Scalp      [priority 5]   5/15M, 25x
Phase 6  Crypto Gambling   [priority 6]   5/15M, 100x, memecoin pump
Phase 7+ Agentic perimeter (postmortem, research, news, tuner)
Phase 8+ Options (deferred)
```

---

## Repo Layout (one-line orientation)

```
src/core/          shared plumbing (config, db, models, logging, clock)
src/scanner/       Goal 2 framework — filters + rankers, pluggable
src/strategies/    one folder per priority; each has policy.yaml
src/brain/         regime classifier (HMM) — added in Phase 2+
src/allocator/     Goal 4 — top-level capital split across strategies
src/safety/        breakers + kill switch
src/brokers/       broker adapters behind a common interface
src/data_sources/  Binance / Delta / Kite market data behind common interface
src/order_manager/ idempotent orders, reconciler
src/dashboard/     FastAPI + HTMX read-only UI
src/entrypoints/   run_bot.py / run_dashboard.py / run_scheduler.py
docs/              PHASES.md, DECISIONS.md, runbook
ops/               Dockerfiles, Railway config
```
