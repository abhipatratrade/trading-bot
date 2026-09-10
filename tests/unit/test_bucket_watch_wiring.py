"""Every BucketWatch field is derived from the bucket, not typed by hand.

``derivatives`` (Decision 036) selects signed ownership and short-aware
holdings inside the invariants. It was added to the dataclass, given tests,
and never written at its one construction site — where it defaulted to False
for every bucket, leaving the entire path dead. commodity-indian's real
NATGASMINI short on 2026-09-04 was then reported by ``foreign_positions`` as
one of the user's own manual trades.

These run against the REAL buckets.yaml. A bucket that gains a capability and
not its check fails here.
"""

from __future__ import annotations

from src.entrypoints.run_bot import bucket_watch_for
from src.shared.bucket import load_bucket


def _watch(bucket_id: str):
    return bucket_watch_for(load_bucket(bucket_id), 60)


# ── the flag that was never wired ───────────────────────────────────────


def test_a_futures_bucket_is_marked_as_trading_derivatives() -> None:
    """commodity-indian routes signals into MCX contracts and holds shorts."""
    assert _watch("commodity-indian").derivatives is True


def test_cash_equity_buckets_are_not() -> None:
    """Their checks must stay byte-identical to the pre-036 behaviour."""
    assert _watch("swing-indian").derivatives is False
    assert _watch("intraday-indian").derivatives is False


def test_derivatives_tracks_the_bucket_rather_than_a_literal() -> None:
    """The failure was a hand-written argument list drifting from the config.
    Whatever the bucket says, the watch must say."""
    for bucket_id in ("commodity-indian", "swing-indian", "intraday-indian"):
        bucket = load_bucket(bucket_id)
        assert (
            bucket_watch_for(bucket, 60).derivatives
            == bucket.trades_derivatives()
        )


# ── the fields that were already wired stay wired ───────────────────────


def test_only_a_same_day_product_is_square_off_checked() -> None:
    """swing-indian carries MTF across days and must never be checked."""
    assert _watch("intraday-indian").intraday is True
    assert _watch("swing-indian").intraday is False


def test_the_notional_budget_is_capital_times_leverage() -> None:
    bucket = load_bucket("commodity-indian")
    cfg = bucket.config
    assert (
        _watch("commodity-indian").notional_budget_inr
        == cfg.capital_inr * cfg.leverage_max
    )


def test_the_bucket_id_and_tick_interval_are_carried_through() -> None:
    watch = bucket_watch_for(load_bucket("commodity-indian"), 97)
    assert watch.bucket_id == "commodity-indian"
    assert watch.tick_interval_seconds == 97


# ── Decision 038: the venue whose session governs stop coverage ─────────


def test_each_indian_bucket_carries_its_own_venue() -> None:
    """MCX and NSE do not share a session — 23:30 IST against 15:30 — and the
    two buckets share a Dhan account, so an account-level answer would be wrong
    for one of them for eight hours a day."""
    assert _watch("commodity-indian").venue == "MCX"
    assert _watch("swing-indian").venue == "NSE"
    assert _watch("intraday-indian").venue == "NSE"


def test_a_crypto_bucket_has_no_venue() -> None:
    """Delta never closes, so a missing stop there is always a fault and must
    keep HALTing. `config.exchange` defaults to "NSE", so reading it bare would
    have marked crypto shut every night."""
    assert _watch("longterm-crypto").venue is None


def test_venue_tracks_the_bucket_rather_than_a_literal() -> None:
    """Same guard as `derivatives` above: whatever buckets.yaml says, the watch
    must say. A silently-defaulted safety flag is not detectable by reading
    `main`."""
    from src.shared.bucket import Market, load_bucket

    for bucket_id in ("commodity-indian", "swing-indian", "intraday-indian"):
        bucket = load_bucket(bucket_id)
        assert bucket.market == Market.INDIAN
        assert bucket_watch_for(bucket, 60).venue == bucket.config.exchange
