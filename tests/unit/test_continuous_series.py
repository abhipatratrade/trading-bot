"""The continuous front-month series — Phase 12b.

The CCI gas run was validated on TradingView's ``NATGASMINI1!``: front month by
EXPIRY, spliced unadjusted when the front expires. The live bot replayed the
EXECUTION contract's own history, which the 15-day floor rolls fifteen days
early. 2026-09-10..16: the engine went short on September, the bot bought
October. These pin the series the bot signals on to the one the run used.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.data_sources.base import OHLCVBar
from src.shared import continuous as cont
from src.shared.continuous import (
    FrontWindow,
    continuous_bars,
    front_by_expiry,
    roll_windows,
    splice,
)

IST = timezone(timedelta(hours=5, minutes=30))

SEP = "NATGASMINI-20260925-FUT"
OCT = "NATGASMINI-20261027-FUT"
NOV = "NATGASMINI-20261125-FUT"
AUG = "NATGASMINI-20260826-FUT"
EXP = {
    AUG: date(2026, 8, 26),
    SEP: date(2026, 9, 25),
    OCT: date(2026, 10, 27),
    NOV: date(2026, 11, 25),
}


def _bar(day: date, hhmm: str, px: str) -> OHLCVBar:
    h, m = hhmm.split(":")
    ts = datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)
    p = Decimal(px)
    return OHLCVBar(timestamp=ts, open=p, high=p, low=p, close=p, volume=Decimal("1"))


# ── front_by_expiry: the TradingView 1! rule ────────────────────────────


def test_front_is_the_nearest_unexpired():
    assert front_by_expiry(EXP, date(2026, 9, 16)) == SEP
    assert front_by_expiry(EXP, date(2026, 9, 26)) == OCT


def test_the_expiring_contract_is_still_front_on_its_expiry_date():
    """>= not >: TV keeps it through expiry and switches the next session."""
    assert front_by_expiry(EXP, date(2026, 9, 25)) == SEP


def test_no_live_contract_is_none():
    assert front_by_expiry(EXP, date(2026, 12, 1)) is None


def test_the_floor_does_not_exist_here():
    """This is what separates the SIGNAL series from execution. On 09-10 the
    15-day floor already executes October; the front is still September."""
    assert front_by_expiry(EXP, date(2026, 9, 10)) == SEP


# ── roll_windows ────────────────────────────────────────────────────────


def test_windows_group_consecutive_dates_on_one_contract():
    ws = roll_windows(EXP, date(2026, 9, 20), date(2026, 10, 2))
    assert ws == [
        FrontWindow(SEP, date(2026, 9, 25), date(2026, 9, 20), date(2026, 9, 25)),
        FrontWindow(OCT, date(2026, 10, 27), date(2026, 9, 26), date(2026, 10, 2)),
    ]


def test_windows_skip_dates_past_the_last_expiry():
    ws = roll_windows({SEP: EXP[SEP]}, date(2026, 9, 24), date(2026, 9, 27))
    assert ws == [FrontWindow(SEP, EXP[SEP], date(2026, 9, 24), date(2026, 9, 25))]


# ── splice ──────────────────────────────────────────────────────────────


def test_splice_takes_each_window_from_its_own_contract_unadjusted():
    """The ~Rs 14 calendar spread stays in the series — it was in the run's."""
    bars = {
        SEP: [_bar(date(2026, 9, 25), "23:15", "279"), _bar(date(2026, 9, 26), "09:00", "280")],
        OCT: [_bar(date(2026, 9, 25), "23:15", "293"), _bar(date(2026, 9, 26), "09:00", "294")],
    }
    ws = roll_windows(EXP, date(2026, 9, 25), date(2026, 9, 26))
    out = splice(bars, ws)
    assert [str(b.close) for b in out] == ["279", "294"]  # Sep's 25th, Oct's 26th


def test_splice_drops_a_contracts_bars_outside_its_tenure():
    """October traded on the 20th too — as the SECOND month. Not in the series."""
    bars = {OCT: [_bar(date(2026, 9, 20), "10:00", "290")]}
    ws = roll_windows(EXP, date(2026, 9, 20), date(2026, 9, 20))  # front = SEP
    assert splice(bars, ws) == []


# ── continuous_bars: I/O with a fake store ──────────────────────────────


class _Store:
    def __init__(self, known: dict[str, list[OHLCVBar]] | None = None) -> None:
        self.rows: dict[tuple[str, str], list[OHLCVBar]] = {
            (s, "15m"): b for s, b in (known or {}).items()
        }
        self.saved: list[tuple[str, int]] = []

    def load(self, symbol, tf):
        return list(self.rows.get((symbol, tf), []))

    def save(self, symbol, tf, bars):
        bars = list(bars)
        self.rows.setdefault((symbol, tf), []).extend(bars)
        self.saved.append((symbol, len(bars)))
        return len(bars)

    def known_contracts(self, underlying, tf):
        return sorted({s for (s, t) in self.rows if t == tf and s.startswith(underlying)})


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=IST)


def _sep_bars():
    return [_bar(date(2026, 9, 24), "10:00", "278"), _bar(date(2026, 9, 25), "10:00", "279")]


def _oct_bars():
    return [
        _bar(date(2026, 9, 24), "10:00", "292"),
        _bar(date(2026, 9, 25), "10:00", "293"),
        _bar(date(2026, 9, 26), "10:00", "294"),
        _bar(date(2026, 9, 26), "11:45", "295"),  # forming at NOW=12:00? no: closes 12:00 — kept
        _bar(date(2026, 9, 26), "12:00", "296"),  # forming — dropped
    ]


def test_the_day_after_expiry_the_old_leg_comes_from_the_store(monkeypatch):
    """The registry no longer lists September. Only the cache has its bars."""
    monkeypatch.setattr(cont, "_written_through", {})
    monkeypatch.setattr(cont, "_warned_missing", set())
    store = _Store({SEP: _sep_bars()})
    fetched: list[str] = []

    def fetch(sym):
        fetched.append(sym)
        return _oct_bars() if sym == OCT else []

    bars, windows = continuous_bars(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=3, now=NOW,
        live_expiries={OCT: EXP[OCT]},  # September is gone from the master
        fetch=fetch, store=store,
    )
    assert fetched == [OCT]  # never asks the venue for an expired contract
    assert [w.symbol for w in windows] == [SEP, OCT]
    assert [str(b.close) for b in bars] == ["278", "279", "294", "295"]


def test_the_live_leg_is_written_through_completed_bars_only(monkeypatch):
    monkeypatch.setattr(cont, "_written_through", {})
    store = _Store()
    continuous_bars(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=1, now=NOW,
        live_expiries={OCT: EXP[OCT]}, fetch=lambda s: _oct_bars(), store=store,
    )
    assert store.saved == [(OCT, 4)]  # the 12:00 bar is still forming
    assert all(b.timestamp < NOW for b in store.load(OCT, "15m"))


def test_a_second_tick_does_not_rewrite_the_same_bars(monkeypatch):
    monkeypatch.setattr(cont, "_written_through", {})
    store = _Store()
    kw = dict(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=1, now=NOW,
        live_expiries={OCT: EXP[OCT]}, fetch=lambda s: _oct_bars(), store=store,
    )
    continuous_bars(**kw)
    continuous_bars(**kw)
    assert store.saved == [(OCT, 4)]


def test_a_new_bar_next_tick_is_the_only_thing_saved(monkeypatch):
    monkeypatch.setattr(cont, "_written_through", {})
    store = _Store()
    kw = dict(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=1,
        live_expiries={OCT: EXP[OCT]}, fetch=lambda s: _oct_bars(), store=store,
    )
    continuous_bars(now=NOW, **kw)
    continuous_bars(now=NOW + timedelta(minutes=15), **kw)  # 12:00 bar settles
    assert store.saved == [(OCT, 4), (OCT, 1)]


def test_a_contract_nobody_remembers_is_bridged_by_the_next_one(monkeypatch):
    """First deployment: the store is empty and September has expired, so
    neither side knows it ever existed. October is then the nearest unexpired
    contract on the 24th and 25th too, and its bars from those days — when it
    was really the second month — fill the gap. Smoother than the run's series
    (no splice jump), not spikier, and it converges: from the next roll on the
    outgoing leg is in the cache before it is needed."""
    monkeypatch.setattr(cont, "_written_through", {})
    monkeypatch.setattr(cont, "_warned_missing", set())
    store = _Store()
    kw = dict(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=3, now=NOW,
        live_expiries={OCT: EXP[OCT]}, fetch=lambda s: _oct_bars(), store=store,
    )
    bars, windows = continuous_bars(**kw)
    assert [w.symbol for w in windows] == [OCT]
    assert [str(b.close) for b in bars] == ["292", "293", "294", "295"]


def test_a_known_but_uncached_expired_leg_is_a_hole_warned_once(monkeypatch, caplog):
    """The store REMEMBERS August existed (from an earlier partial write) but
    holds no bars for the window: that leg is a hole, reported once."""
    monkeypatch.setattr(cont, "_written_through", {})
    monkeypatch.setattr(cont, "_warned_missing", set())
    store = _Store({SEP: _sep_bars()})
    store.rows[(AUG, "15m")] = []  # known, empty
    kw = dict(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=40, now=NOW,
        live_expiries={OCT: EXP[OCT]}, fetch=lambda s: _oct_bars(), store=store,
    )
    bars, windows = continuous_bars(**kw)
    continuous_bars(**kw)  # second tick
    assert [w.symbol for w in windows] == [AUG, SEP, OCT]
    assert [str(b.close) for b in bars] == ["278", "279", "294", "295"]
    assert cont._warned_missing == {AUG}


def test_the_store_contributes_expiries_the_registry_forgot(monkeypatch):
    """A cached contract's expiry is read back out of its symbol."""
    monkeypatch.setattr(cont, "_written_through", {})
    monkeypatch.setattr(cont, "_warned_missing", set())
    store = _Store({SEP: _sep_bars()})
    _, windows = continuous_bars(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=3, now=NOW,
        live_expiries={OCT: EXP[OCT]}, fetch=lambda s: _oct_bars(), store=store,
    )
    assert windows[0] == FrontWindow(SEP, date(2026, 9, 25), date(2026, 9, 23), date(2026, 9, 25))


def test_a_failed_fetch_of_the_live_leg_yields_what_the_others_have(monkeypatch):
    monkeypatch.setattr(cont, "_written_through", {})
    monkeypatch.setattr(cont, "_warned_missing", set())
    store = _Store({SEP: _sep_bars()})

    def boom(sym):
        raise RuntimeError("DH-905")

    bars, _ = continuous_bars(
        underlying="NATGASMINI", tf="15m", tf_minutes=15, days=3, now=NOW,
        live_expiries={OCT: EXP[OCT]}, fetch=boom, store=store,
    )
    assert [str(b.close) for b in bars] == ["278", "279"]


def test_the_september_divergence_reproduced():
    """09-16: the floor executes October; the front — and the run — is September."""
    on = date(2026, 9, 16)
    assert front_by_expiry(EXP, on) == SEP
    # The floor's answer, for contrast: nearest expiry >= 15 days out.
    floor = min(s for s, e in EXP.items() if (e - on).days >= 15)
    assert floor == OCT
