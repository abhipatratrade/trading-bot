"""
A continuous front-month series from per-contract bars (Phase 12b).

WHY THIS EXISTS. commodity-indian's CCI machine replays 90 days of ONE
contract's bars — whichever contract the 15-day roll floor has selected for
execution. The 125-trade run it was validated against did no such thing: it
ran on TradingView's ``NATGASMINI1!``, a continuous series that shows the
front month BY EXPIRY and splices the next one in, unadjusted, when the front
expires. Two consequences, both seen live in September 2026:

* The floor rolled execution to October on 09-10, fifteen days before
  September expired. From that tick the machine reasoned over October's own
  history — a different instrument at ~Rs 14 premium — while the engine was
  still on September. The engine went short on 09-14; the bot found a long
  on 09-16. Opposite books, each correct on its own inputs.
* Even once both are on October, a 90-day replay of October-only bars is not
  the spliced series: the armed flags (which never expire) were set on
  September's prints, and October's prints from when it was the SECOND month
  are not what the engine ever saw.

So the signal series and the execution contract are two different questions.
This module answers the first the way the backtest did: for each session
date, the contract with the nearest expiry on or after that date is the front;
its bars are that date's bars; consecutive dates on the same contract form a
window; windows concatenate with no adjustment. The strategy signals on THAT
and executes on whatever the floor selects.

WHY A STORE. Dhan's scrip master drops a contract the day it expires, and with
it the ability to fetch that contract's bars. The pre-roll leg of the series is
therefore unobtainable from the live API a day after every roll — exactly when
it matters most, because that is when the armed state was set. Every live fetch
of a contract is written through to ``contract_bar``; the leg that can no
longer be fetched is read back from there. Completed bars only: a forming bar
changes, and a cache of prices that move is worse than none.

The pure functions here take plain mappings and lists so they test without a
registry, a broker or a database. ``continuous_bars`` is the only I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from src.core.logging import get_logger
from src.data_sources.base import OHLCVBar
from src.shared.bars import completed_bars
from src.shared.contracts import parse_contract_symbol

_log = get_logger("shared.continuous")

# Expired contracts already reported as uncached, so the warning fires once.
_warned_missing: set[str] = set()

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True, slots=True)
class FrontWindow:
    """One contract's tenure as the front month: ``start``..``end`` inclusive, IST dates."""

    symbol: str
    expiry: date
    start: date
    end: date


def front_by_expiry(expiries: Mapping[str, date], on: date) -> str | None:
    """The contract that is front ON ``on``: nearest expiry >= ``on``.

    ``>=``, not ``>``: TradingView keeps the expiring contract as the front
    through its expiry date and switches at the next session. Ties (two
    contracts, one expiry) cannot happen for one underlying's futures.
    """
    live = [(exp, sym) for sym, exp in expiries.items() if exp >= on]
    if not live:
        return None
    return min(live)[1]


def roll_windows(expiries: Mapping[str, date], start: date, end: date) -> list[FrontWindow]:
    """Which contract is front on each date in ``start``..``end``, grouped.

    Dates with no live contract (the universe ended) are skipped, which can
    only happen at the far end of a window that outruns the listed expiries.
    """
    out: list[FrontWindow] = []
    d = start
    while d <= end:
        sym = front_by_expiry(expiries, d)
        if sym is not None:
            if out and out[-1].symbol == sym:
                out[-1] = FrontWindow(sym, out[-1].expiry, out[-1].start, d)
            else:
                out.append(FrontWindow(sym, expiries[sym], d, d))
        d += timedelta(days=1)
    return out


def splice(
    bars_by_symbol: Mapping[str, Iterable[OHLCVBar]],
    windows: Iterable[FrontWindow],
    *,
    tz: timezone = IST,
) -> list[OHLCVBar]:
    """Raw front-month splice: each window's dates from its own contract.

    No back-adjustment. The discontinuity at a roll is the calendar spread and
    it is IN the series the backtest ran on, so it stays in this one. A bar is
    assigned to a date by its stamp in ``tz`` — MCX sessions (09:00-23:30 IST)
    never straddle midnight, so this is unambiguous.
    """
    out: list[OHLCVBar] = []
    for w in windows:
        for b in sorted(bars_by_symbol.get(w.symbol, ()), key=lambda b: b.timestamp):
            if w.start <= b.timestamp.astimezone(tz).date() <= w.end:
                out.append(b)
    return out


class BarStore(Protocol):
    """Where completed contract bars outlive the contract (``contract_bar``)."""

    def load(self, symbol: str, tf: str) -> list[OHLCVBar]: ...

    def save(self, symbol: str, tf: str, bars: Iterable[OHLCVBar]) -> int: ...

    def known_contracts(self, underlying: str, tf: str) -> list[str]: ...


def continuous_bars(
    *,
    underlying: str,
    tf: str,
    tf_minutes: int,
    days: int,
    now: datetime,
    live_expiries: Mapping[str, date],
    fetch: Callable[[str], list[OHLCVBar]],
    store: BarStore | None,
) -> tuple[list[OHLCVBar], list[FrontWindow]]:
    """The spliced front-month series for the last ``days`` days, and its windows.

    ``live_expiries`` is what the registry lists today (expired contracts are
    already gone from it); the store contributes the contracts it remembers,
    with their expiry read back out of the symbol. ``fetch`` is the live
    history call for one contract symbol and is only made for contracts the
    registry still lists — a fetch for an expired one can only fail.

    Returns the windows too, so a caller can name the contract each bar came
    from (``signal_contract`` in the entry hint) and so a test can see the
    roll it expects.
    """
    expiries: dict[str, date] = dict(live_expiries)
    if store is not None:
        for sym in store.known_contracts(underlying, tf):
            key = parse_contract_symbol(sym)
            if key is not None and sym not in expiries:
                expiries[sym] = key.expiry

    today = now.astimezone(IST).date()
    windows = roll_windows(expiries, today - timedelta(days=days), today)
    if not windows:
        _log.warning("continuous_no_front_contract", underlying=underlying, on=str(today))
        return [], []

    bars_by_symbol: dict[str, list[OHLCVBar]] = {}
    for w in windows:
        if w.symbol in live_expiries:
            try:
                raw = fetch(w.symbol)
            except Exception:
                _log.warning("continuous_fetch_failed", contract=w.symbol, exc_info=True)
                raw = []
            done = completed_bars(raw, minutes=tf_minutes, now=now)
            if store is not None and done:
                _write_through(store, w.symbol, tf, done)
            bars_by_symbol[w.symbol] = done
            continue
        # Expired: the registry cannot fetch it; only the store remembers it.
        cached = store.load(w.symbol, tf) if store is not None else []
        if not cached and w.symbol not in _warned_missing:
            # Once per contract per process. Only a contract SOMEONE remembers
            # can be a hole — one the registry has dropped and the store never
            # wrote is not in ``expiries`` at all, so the next contract is
            # simply front for those dates too (its own earlier bars bridge
            # them; smoother than the run's splice, not spikier). Each roll
            # from now on is cached before it is needed.
            _warned_missing.add(w.symbol)
            _log.warning(
                "continuous_leg_unavailable",
                contract=w.symbol,
                window=f"{w.start}..{w.end}",
                hint="expired and never cached — the series starts after it",
            )
        bars_by_symbol[w.symbol] = cached

    return splice(bars_by_symbol, windows), windows


# (symbol, tf) -> newest bar stamp already written through, per process. The
# live leg is refetched every tick (~90s) and is ~5,000 bars; without this the
# same rows would be upserted every tick for nothing. Cleared by a restart,
# after which one full (idempotent) write re-establishes it.
_written_through: dict[tuple[str, str], datetime] = {}


def _write_through(store: BarStore, symbol: str, tf: str, done: list[OHLCVBar]) -> None:
    """Save only bars newer than the last one this process already saved."""
    since = _written_through.get((symbol, tf))
    fresh = [b for b in done if since is None or b.timestamp > since]
    if not fresh:
        return
    try:
        store.save(symbol, tf, fresh)
    except Exception:
        # The cache is a convenience for the NEXT roll; a failed write must
        # not cost this tick its signal. Not marking as written either, so the
        # next tick retries.
        _log.warning("continuous_store_save_failed", contract=symbol, exc_info=True)
        return
    _written_through[(symbol, tf)] = max(b.timestamp for b in fresh)


class PostgresBarStore:
    """``BarStore`` over the ``contract_bar`` table. Upsert on save, never update.

    ON CONFLICT DO NOTHING rather than DO UPDATE: only completed bars are ever
    written, and a completed bar does not change. If a vendor ever re-served a
    settled bar differently the FIRST reading stays, which is also what the
    engine's cached CSVs hold.
    """

    def load(self, symbol: str, tf: str) -> list[OHLCVBar]:
        from sqlalchemy import select

        from src.core.db import session_scope
        from src.core.models import ContractBar

        with session_scope() as session:
            rows = session.execute(
                select(ContractBar)
                .where(ContractBar.symbol == symbol, ContractBar.tf == tf)
                .order_by(ContractBar.ts)
            ).scalars()
            return [
                OHLCVBar(
                    timestamp=r.ts,
                    open=r.open,
                    high=r.high,
                    low=r.low,
                    close=r.close,
                    volume=r.volume,
                )
                for r in rows
            ]

    def save(self, symbol: str, tf: str, bars: Iterable[OHLCVBar]) -> int:
        from sqlalchemy.dialects.postgresql import insert

        from src.core.db import session_scope
        from src.core.models import ContractBar

        payload = [
            {
                "symbol": symbol,
                "tf": tf,
                "ts": b.timestamp,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
        if not payload:
            return 0
        with session_scope() as session:
            result = session.execute(
                insert(ContractBar)
                .values(payload)
                .on_conflict_do_nothing(constraint="uq_contract_bar")
            )
            return int(result.rowcount or 0)

    def known_contracts(self, underlying: str, tf: str) -> list[str]:
        from sqlalchemy import distinct, select

        from src.core.db import session_scope
        from src.core.models import ContractBar

        with session_scope() as session:
            syms = session.execute(
                select(distinct(ContractBar.symbol)).where(
                    ContractBar.tf == tf,
                    ContractBar.symbol.startswith(f"{underlying}-"),
                )
            ).scalars()
            return sorted(syms)
