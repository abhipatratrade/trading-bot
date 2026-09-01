"""Dhan's symbol spelling must be translated back to the bot's (2026-09-01).

THE INCIDENT. commodity-indian opened a live NATGASMINI future. The bot minted,
ordered and stored ``NATGASMINI-20260925-FUT``; Dhan echoed the position back as
``NATGASMINI-25Sep2026-FUT``. Nothing joined the two, so ``net_owned`` could not
recognise the bot's own position and it read as FOREIGN. Every safety layer
then did exactly what it is designed to do with someone else's position — which
is nothing:

* the stop sweep placed no protective stop (Decision 027 leaves foreign
  positions strictly alone), so the lot sat unhedged;
* stop-coverage raised no alarm, because it was not the bot's position to cover;
* the reconciler never matched it, so the trade stayed PENDING with no
  ``position`` row;
* the strategy could never exit it — ``select_exits`` iterates held positions,
  and there were none;
* and with no position row the bucket believed it was FLAT, so the next signal
  fifteen minutes later opened a SECOND lot.

``securityId`` is the one identifier both sides agree on, so translation keys on
that. These tests pin the translation and, just as importantly, that cash equity
— which injects no translator — is completely unaffected.
"""

from __future__ import annotations

from decimal import Decimal

from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanClient

_CANON = "NATGASMINI-20260925-FUT"
_DHAN = "NATGASMINI-25Sep2026-FUT"
_SEC = "568246"


def _client(*, translate: bool = True) -> DhanClient:
    return DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=lambda s: (_SEC, "MCX_COMM"),
        canonical_symbol=(
            (lambda sid: _CANON if sid == _SEC else None) if translate else None
        ),
    )


def test_dhan_spelling_becomes_the_bots_symbol() -> None:
    got = _client()._symbol_of({"tradingSymbol": _DHAN, "securityId": _SEC})
    assert got == _CANON, "the ledger and the venue must agree on one string"


def test_unknown_security_id_falls_back_to_dhans_symbol() -> None:
    """A contract the registry has never heard of must still parse."""
    got = _client()._symbol_of({"tradingSymbol": "SOMETHING", "securityId": "999"})
    assert got == "SOMETHING"


def test_cash_equity_is_untouched() -> None:
    """No translator injected: Dhan's tradingSymbol IS the bot's ticker."""
    got = _client(translate=False)._symbol_of(
        {"tradingSymbol": "SWIGGY", "securityId": "1001"}
    )
    assert got == "SWIGGY"


def test_a_broken_translator_cannot_break_parsing() -> None:
    """Fail soft. A raising lookup must degrade to the old behaviour, not
    take down every position/order parse on the account."""
    client = DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=lambda s: (_SEC, "MCX_COMM"),
        canonical_symbol=lambda sid: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert client._symbol_of({"tradingSymbol": _DHAN, "securityId": _SEC}) == _DHAN


def test_missing_security_id_still_parses() -> None:
    assert _client()._symbol_of({"tradingSymbol": _DHAN}) == _DHAN
    assert _client()._symbol_of({}) == ""


# ── the property that actually mattered ─────────────────────────────────


class _Resp:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeHttp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def request(self, method, url, json=None, headers=None):  # noqa: A002, ARG002
        return _Resp(self._payload)


def test_position_from_dhan_carries_the_bots_symbol() -> None:
    """End to end through the real parser, exactly as the live position came.

    This is the assertion that would have caught the incident. A position as
    Dhan reports it must come back keyed by the symbol the LEDGER stores, or
    ownership matching fails silently and the position is orphaned at birth —
    unstopped, unexitable, and invisible to the bucket that opened it.
    """
    client = DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=lambda s: (_SEC, "MCX_COMM"),
        canonical_symbol=lambda sid: _CANON if sid == _SEC else None,
        http=_FakeHttp([
            {
                "tradingSymbol": _DHAN,
                "securityId": _SEC,
                "netQty": "2",
                "buyAvg": "277.20",
                "productType": "MARGIN",
            }
        ]),
    )

    positions = client._positions_only()

    assert len(positions) == 1
    assert positions[0].symbol == _CANON, "must match what the ledger stored"
    assert positions[0].side == "long"
    assert positions[0].size == Decimal("2")
    assert positions[0].entry_price == Decimal("277.20")
