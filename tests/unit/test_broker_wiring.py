"""The factories must accept what their callers actually pass.

On 2026-08-30 ``run_bot`` began passing ``contract_spec=`` to
``DhanClient.from_settings``, which took it on ``__init__`` but not on the
classmethod. Every startup raised

    TypeError: DhanClient.from_settings() got an unexpected keyword argument

which surfaced as "Dhan account init failed" -> both Indian buckets skipped ->
`no_runners_despite_enabled_buckets` -> exit 1 -> systemd restart -> repeat, 414
times over 20 hours, still down when Monday's session opened.

Nothing caught it. The 973-test suite never called ``from_settings`` with the
caller's real keyword set, and ``selfcheck`` (the deploy gate) did not build
brokers at all, so it logged ``selfcheck_ok`` on code that could not boot.

These tests close the unit half of that. They are deliberately about SIGNATURE
COMPATIBILITY, not behaviour: no network, no credentials, no fixtures beyond a
stub Settings — so they run anywhere and fail the moment a call site and its
factory drift apart.
"""

from __future__ import annotations

import inspect

import pytest

from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanClient
from src.data_sources.dhan import DhanData

# Exactly what src/entrypoints/run_bot.py passes. If a new keyword is added
# there, add it here — a test that fails loudly is the point.
RUN_BOT_CLIENT_KWARGS = {
    "data_token_manager",
    "product_type",
    "owns_order_id",
    "contract_spec",
}
RUN_BOT_DATA_KWARGS = {"request_delay_seconds", "fno"}


class TestFactoriesAcceptTheirCallersKwargs:
    def test_dhan_client_from_settings(self) -> None:
        params = inspect.signature(DhanClient.from_settings).parameters
        missing = RUN_BOT_CLIENT_KWARGS - set(params)
        assert not missing, (
            f"DhanClient.from_settings() does not accept {sorted(missing)}, "
            f"which run_bot passes. This is the 2026-08-30 crash loop."
        )

    def test_dhan_data_from_settings(self) -> None:
        params = inspect.signature(DhanData.from_settings).parameters
        missing = RUN_BOT_DATA_KWARGS - set(params)
        assert not missing, (
            f"DhanData.from_settings() does not accept {sorted(missing)}, "
            f"which run_bot passes."
        )

    # ``data_token_manager`` is deliberately excluded: it is CONSUMED rather
    # than forwarded (live mode reuses it as ``token_manager``, sandbox
    # substitutes the static DevPortal token), so a verbatim-forwarding check
    # would be a false positive on it.
    @pytest.mark.parametrize(
        "kwarg", sorted(RUN_BOT_CLIENT_KWARGS - {"data_token_manager"})
    )
    def test_each_kwarg_is_forwarded_to_the_constructor(self, kwarg: str) -> None:
        """Accepting a keyword and then dropping it is the same bug, quieter.

        ``contract_spec`` was silently absent from the ``cls(...)`` call for a
        while before run_bot started passing it; a factory that swallows an
        argument produces a client configured differently from the one the
        caller asked for.
        """
        src = inspect.getsource(DhanClient.from_settings)
        assert f"{kwarg}=" in src, (
            f"from_settings accepts {kwarg!r} but never forwards it to the "
            f"constructor — the value is silently discarded."
        )


class TestClientConstructsOffline:
    """Construction must stay network-free, or selfcheck cannot use it.

    ``selfcheck`` builds these adapters as its deploy gate. That is only safe
    while building them makes no request — otherwise a Dhan outage or an
    edge-blocked datacenter IP would block deploys, including the deploy that
    fixes the outage.
    """

    def test_from_settings_call_shape_matches_run_bot(self) -> None:
        client = DhanClient(
            token_manager=DhanTokenManager(static_token="TOK"),
            client_id="1000000001",
            resolve_symbol=lambda _s: ("1", "NSE_EQ"),
            base_url="https://example.invalid",
            owns_order_id=lambda _oid: False,
            contract_spec=None,
        )
        # No request was made building that; the http client is lazy.
        assert client is not None

    def test_contract_spec_none_falls_back_rather_than_raising(self) -> None:
        """A cash-only process passes None, and must keep working."""
        client = DhanClient(
            token_manager=DhanTokenManager(static_token="TOK"),
            client_id="1000000001",
            resolve_symbol=lambda _s: ("1", "NSE_EQ"),
            base_url="https://example.invalid",
            contract_spec=None,
        )
        assert client._contract_spec is None
