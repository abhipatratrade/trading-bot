"""The runner squares off on the wall clock — Phase 12c.

intraday-indian's strategy squares off when the latest 5m bar is stamped
>= 15:15. Dhan's 5m feed has ended at a 15:10 stamp on every session since
2026-08-03, so that has been unsatisfiable on every session the bucket has
traded: CASTROLIND, IIFL and COFORGE carry no square-off row at all, and
PPLPHARMA's one 15:18 attempt was refused. Dhan's MIS auto-square-off closed
every one of them; the CNC fallback (Decision 031) has no such net.

The backstop lives in the RUNNER, where the clock is, so the validated strategy
stays bar-driven and replays identically (House Rule 9).
"""

from __future__ import annotations

import inspect
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.models import Position, PositionSide
from src.shared.bucket import Bucket, BucketConfig, Market, TradingType
from src.shared.bucket_runner import BucketRunner
from src.shared.market_calendar import IST

_SCANNER_YAML = (
    "universe_size: 5\nranker:\n  name: volume_24h_desc\n  params: {}\n"
)
_ALLOCATOR_YAML = "fractional_kelly: 0.25\n"
_REGIME_YAML = (
    "enabled: false\ntf: 1d\ntraining_window_days: 30\n"
    "inference_lookback_bars: 50\n"
)
_HEADER = (
    "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,"
    "trading_type,scanner\n"
)


class _Clock:
    def __init__(self, hhmm: str) -> None:
        h, m = hhmm.split(":")
        # 2026-09-09 is a Wednesday — the COFORGE session.
        self._now = datetime(2026, 9, 9, int(h), int(m), 30, tzinfo=IST)

    def now(self) -> datetime:
        return self._now


def _bucket(folder: Path, *, squareoff: str | None) -> Bucket:
    return Bucket(
        id="intraday-indian",
        trading_type=TradingType.INTRADAY,
        market=Market.INDIAN,
        config=BucketConfig(
            capital_inr=Decimal("50000"),
            broker="dhan",
            leverage_max=Decimal("5"),
            product="INTRADAY",
            squareoff=squareoff,
        ),
        folder=folder,
    )


def _write_bucket(folder: Path) -> None:
    (folder / "strategies").mkdir(parents=True)
    (folder / "scanner.yaml").write_text(_SCANNER_YAML)
    (folder / "allocator.yaml").write_text(_ALLOCATOR_YAML)
    (folder / "regime.yaml").write_text(_REGIME_YAML)
    (folder / "strategy_master.csv").write_text(_HEADER)


def _pos(symbol: str, qty: str) -> Position:
    return Position(
        bucket_id="intraday-indian",
        strategy_id="intraday-indian",
        strategy_name="gap_down_reversal_broad",
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal(qty),
        entry_price=Decimal("100"),
    )


@pytest.fixture
def make_runner(tmp_path, monkeypatch):
    def _make(*, at: str, squareoff: str | None = "15:15"):
        _write_bucket(tmp_path)
        closed: list[str] = []
        monkeypatch.setattr(
            BucketRunner,
            "_close_position",
            lambda self, om, strat, pos, regime: closed.append(pos.symbol) or True,
        )
        runner = BucketRunner(
            bucket=_bucket(tmp_path, squareoff=squareoff),
            brokers={"default": object()},  # type: ignore[dict-item]
            data=object(),  # type: ignore[arg-type]
            order_managers={"default": object()},  # type: ignore[dict-item]
            clock=_Clock(at),
        )
        return runner, closed

    return _make


HELD = {"gap_down_reversal_broad": {"COFORGE": _pos("COFORGE", "27")}}


def test_before_the_bell_nothing_closes(make_runner):
    runner, closed = make_runner(at="15:14")
    assert runner._run_squareoff(object(), HELD, set()) == 0
    assert closed == []


def test_at_the_bell_everything_held_closes(make_runner):
    """15:15:30 — the first tick after the bell, with no 15:15 bar in sight."""
    runner, closed = make_runner(at="15:15")
    held = {
        "gap_down_reversal_broad": {
            "COFORGE": _pos("COFORGE", "27"),
            "IIFL": _pos("IIFL", "74"),
        }
    }
    assert runner._run_squareoff(object(), held, set()) == 2
    assert sorted(closed) == ["COFORGE", "IIFL"]


def test_a_strategy_exit_already_in_flight_is_not_doubled(make_runner):
    """The strategy fired on time — the backstop must not sell it twice."""
    runner, closed = make_runner(at="15:20")
    exiting = {("gap_down_reversal_broad", "COFORGE")}
    assert runner._run_squareoff(object(), HELD, exiting) == 0
    assert closed == []


def test_a_close_is_recorded_as_exiting_for_the_rest_of_the_pass(make_runner):
    runner, _ = make_runner(at="15:16")
    exiting: set[tuple[str, str]] = set()
    runner._run_squareoff(object(), HELD, exiting)
    assert ("gap_down_reversal_broad", "COFORGE") in exiting


def test_a_bucket_without_a_squareoff_is_never_touched(make_runner):
    """swing-indian carries MTF for days. No config, no square-off, ever."""
    runner, closed = make_runner(at="15:30", squareoff=None)
    assert runner._run_squareoff(object(), HELD, set()) == 0
    assert closed == []


def test_nothing_held_is_a_noop(make_runner):
    runner, closed = make_runner(at="15:16")
    assert runner._run_squareoff(object(), {}, set()) == 0


def test_intraday_indian_is_configured_for_it():
    """15:09, before Dhan's ~15:11:30 MIS auto-square-off — after it Dhan
    refuses new intraday orders, which is how every 15:15 exit was refused
    and tripped the reject_rate kill switch on 2026-09-24 (user decision
    2026-09-25)."""
    from src.shared.bucket import load_buckets

    cfg = {b.id: b.config for b in load_buckets()}
    assert cfg["intraday-indian"].squareoff == "15:09"
    assert cfg["swing-indian"].squareoff is None
    assert cfg["commodity-indian"].squareoff is None


def test_the_backstop_runs_after_strategy_exits():
    """Order matters: a strategy that fires on time takes precedence, and the
    backstop only sees what it left. Pinned in source like the repo pins its
    other load-bearing wiring."""
    src = inspect.getsource(BucketRunner._run_exits)
    assert "self._run_squareoff(om, by_strategy, recent_exit_keys)" in src
    assert src.index("select_exits") < src.index("_run_squareoff")


def test_the_strategy_exit_is_untouched():
    """House Rule 9: the validated strategy still squares off from the BAR.
    The fix is beside it, not inside it."""
    import importlib.util

    path = (
        Path(__file__).resolve().parents[2]
        / "src/strategies/intraday/indian/strategies/gap_down_reversal.py"
    )
    spec = importlib.util.spec_from_file_location("_gdr_sq", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = inspect.getsource(mod.GapDownReversal.select_exits)
    assert "ist_time(latest) >= _SQUAREOFF" in src
    # No clock object, no now(): the decision is a function of the bars alone.
    assert "RealClock" not in src and ".now()" not in src
