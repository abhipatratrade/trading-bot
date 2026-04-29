# trading-bot

Multi-strategy trading system for crypto (Delta Exchange India) and equities
(Zerodha Kite). Deployed on Railway. Designed around a **deterministic core**
with an optional **agentic perimeter** added in later phases.

> The product spec lives in `C:\Users\User\Desktop\Goal_Setting.txt` (the
> "bible"). Architecture decisions live in `docs/DECISIONS.md`. The build
> tracker lives in `docs/PHASES.md`. Claude reads `CLAUDE.md` first.

## Quick orientation

| Path | Purpose |
|---|---|
| `CLAUDE.md` | What Claude reads first when a session opens |
| `docs/PHASES.md` | What's done, what's next — tick boxes per session |
| `docs/DECISIONS.md` | Locked architecture decisions, append-only |
| `src/core/` | Shared plumbing (config, db, models, logging, clock) |
| `src/scanner/` | Goal 2 — pluggable filter + ranker framework |
| `src/strategies/` | One folder per priority; each has `policy.yaml` |
| `src/brokers/` | Delta India + Zerodha adapters behind a common interface |
| `src/data_sources/` | Binance + Delta + Kite market data |
| `src/order_manager/` | Idempotent placement + reconciler |
| `src/safety/` | Breakers + kill switch |
| `src/dashboard/` | FastAPI + HTMX read-only UI |
| `src/entrypoints/` | `run_bot.py`, `run_dashboard.py`, `run_scheduler.py` |

## Resuming work

In a new Claude Code session, just say `continue` — Claude reads
`CLAUDE.md` and `docs/PHASES.md` and picks up the next unchecked item.

## Status

**Phase 0 — Foundations** in progress. See `docs/PHASES.md`.
