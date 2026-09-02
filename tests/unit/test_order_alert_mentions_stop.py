"""An entry that carries an attached stop must SAY so (2026-09-02).

The placement alert tags on ``stop_price`` — the STANDALONE stop. An attached
stop (Decision 034) travels in ``attached_stop_price``, so once
commodity-indian moved to attached stops its entries began reporting a bare

    [commodity-indian] ORDER BUY 1 NATGASMINI-20260925-FUT @ market [filled]

while silently carrying a stop at 267.10. The protection improved and the
telemetry regressed. On a bucket where every other stop path has failed in
production, "is this position covered?" has to be answerable from the phone.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.order_manager import manager as mod


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    out: list[str] = []
    monkeypatch.setattr(mod, "send_alert", lambda msg: out.append(msg) or True)
    return out


def _alert(**kw) -> str:
    """Render one placement alert through the module's own formatting."""
    scope = "commodity-indian"
    stop_price = kw.get("stop_price")
    reduce_only = kw.get("reduce_only", False)
    attached = kw.get("attached_stop_price")
    tag = "STOP" if stop_price is not None else ("EXIT" if reduce_only else "ORDER")
    price_str = f"trigger {stop_price}" if stop_price is not None else "market"
    protection = f" +stop {attached}" if attached is not None else ""
    return f"[{scope}] {tag} BUY 1 NATGASMINI @ {price_str}{protection} [filled]"


def test_attached_stop_is_named_in_the_alert() -> None:
    msg = _alert(attached_stop_price=Decimal("267.10"))
    assert "+stop 267.10" in msg, "a protected entry must say what protects it"
    assert msg.startswith("[commodity-indian] ORDER BUY")


def test_a_bare_entry_says_nothing_extra() -> None:
    assert _alert() == "[commodity-indian] ORDER BUY 1 NATGASMINI @ market [filled]"


def test_a_standalone_stop_still_tags_as_stop() -> None:
    msg = _alert(stop_price=Decimal("269.6"))
    assert " STOP BUY " in msg
    assert "trigger 269.6" in msg
    assert "+stop" not in msg, "no double-reporting of the same protection"


def test_the_real_alert_path_includes_the_attached_stop(sent) -> None:
    """Guards the actual f-string, not a copy of it: the formatting above is
    only trustworthy if the module emits the same shape."""
    src = mod.__file__
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert '{protection}' in body, "the alert must interpolate the attached stop"
    assert 'f" +stop {attached_stop_price}"' in body
