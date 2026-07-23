"""Postgres-backed shared Dhan token store — the cross-VM peer source.

The bot's own processes share one 24h token via an on-disk cache
(``auth.DEFAULT_TOKEN_CACHE_PATH``). That does not reach a process on a
DIFFERENT machine — notably the depth-data recorder on its own VM. Because
Dhan keeps one active token per client id (minting evicts the prior one), two
machines each minting would evict each other ~once a day (2026-07-23).

This store is the cross-machine analogue of the file cache: a single
``dhan_token`` row per client id that the routine minter (the bot) writes and
every other process reads. It plugs into ``DhanTokenManager`` as a
``TokenStore`` (see ``auth.TokenStore``) alongside the file cache.

CONTRACT — both methods are FAIL-SOFT by design. The token manager only ever
touches this on a refresh (once/day) or after a 401, never on the hot per-tick
path, and it always has the local file cache + in-memory last-good token to
fall back to. So a DB outage must degrade silently, never raise into the
trading loop: ``load`` returns None and ``save`` swallows on any error.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.db import get_session_factory, session_scope
from src.core.logging import get_logger
from src.core.models import DhanToken

_log = get_logger("brokers.dhan.token_store")


class PostgresTokenStore:
    """Read/write the shared ``dhan_token`` row for one client id.

    ``minted_by`` is stamped on every write purely for forensics (which
    process last minted). The bot passes "bot"; the recorder passes
    "recorder".
    """

    def __init__(self, client_id: str, *, minted_by: str) -> None:
        self._client_id = client_id
        self._minted_by = minted_by

    def load(self) -> str | None:
        """Return the row's token, or None on a miss OR any DB error.

        Never raises — a DB outage degrades to the local file cache.
        """
        try:
            factory = get_session_factory()
            with factory() as s:
                return s.execute(
                    select(DhanToken.token).where(
                        DhanToken.client_id == self._client_id
                    )
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 — fail-soft: caller falls back
            _log.warning(
                "dhan_token_store_load_failed",
                client_id=self._client_id,
                exc_info=True,
            )
            return None

    def save(self, token: str) -> None:
        """Upsert the shared token for this client id. Never raises.

        Uses INSERT ... ON CONFLICT (client_id) DO UPDATE so the single row is
        atomically replaced by whichever process last minted.
        """
        try:
            stmt = pg_insert(DhanToken).values(
                client_id=self._client_id,
                token=token,
                minted_at=func.now(),
                minted_by=self._minted_by,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["client_id"],
                set_={
                    "token": token,
                    "minted_at": func.now(),
                    "minted_by": self._minted_by,
                    "updated_at": func.now(),
                },
            )
            with session_scope() as s:
                s.execute(stmt)
        except Exception:  # noqa: BLE001 — fail-soft: file cache still holds it
            _log.warning(
                "dhan_token_store_save_failed",
                client_id=self._client_id,
                exc_info=True,
            )
