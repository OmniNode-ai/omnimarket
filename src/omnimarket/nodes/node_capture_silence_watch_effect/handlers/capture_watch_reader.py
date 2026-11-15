# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only Postgres effect boundary with a lazy, reusable connection."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

from omnimarket.handlers.delegation_scenario_settings import load_store_overlay
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    CaptureWatchPolicyError,
    ModelActorNewest,
    ModelCaptureObservation,
    ModelCaptureSource,
    ModelReadsSource,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.password_reference import (
    resolve_password,
)

NODE_NAME = "node_capture_silence_watch_effect"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
_KEYWORD_PASSWORD = re.compile(r"(?:^|\s)password\s*=", re.IGNORECASE)


def validate_identifier(value: str) -> str:
    """Reject SQL fragments before interpolation, even for constructed models."""
    if not _IDENTIFIER.fullmatch(value):
        raise CaptureWatchPolicyError(f"not a plain SQL identifier: {value!r}")
    return value


def dsn_carries_password(dsn: str) -> bool:
    """Whether a URL or keyword DSN already holds a password."""
    return bool(urlsplit(dsn).password) or bool(_KEYWORD_PASSWORD.search(dsn))


def dsn_with_password(dsn: str, password: str) -> str:
    """The DSN with ``password`` added: a URL gets it percent-encoded in its user part, a keyword DSN a quoted ``password`` pair."""
    parts = urlsplit(dsn)
    if parts.scheme in ("postgresql", "postgres"):
        host = parts.netloc.rpartition("@")[2]
        user = quote(parts.username or "", safe="")
        return parts._replace(
            netloc=f"{user}:{quote(password, safe='')}@{host}"
        ).geturl()
    escaped = password.replace("\\", "\\\\").replace("'", "\\'")
    return f"{dsn} password='{escaped}'"


def resolve_reads_source_dsn(
    source: ModelReadsSource,
    *,
    environ: Mapping[str, str] | None = None,
    resolve: Callable[..., str] = resolve_password,
    load_store: Callable[[str], ModelSecretStoreOverlay | None] = load_store_overlay,
    with_password: Callable[[str, str], str] = dsn_with_password,
) -> str:
    """The contract-named DSN, with its password read from the secret store when the unit names a reference.

    Never guesses a database identity. A DSN that carries a password, a contract that names no reference variable, or a unit that
    sets none, is used as given. A reference that does not resolve fails closed (the variable is named, never the reference or a value).
    """
    env = os.environ if environ is None else environ
    dsn = env.get(source.dsn_env, "")
    if not dsn.strip():
        raise CaptureWatchPolicyError(
            f"{source.dsn_env} is unset or empty for binding {source.binding_ref!r}"
        )
    if (
        source.password_ref_env is None
        or dsn_carries_password(dsn)
        or not (env.get(source.password_ref_env, "") or "").strip()
    ):
        return dsn
    reference = env[source.password_ref_env]
    overlay = load_store(NODE_NAME)
    password = resolve(reference, store_overlay=overlay)
    if not password:
        raise CaptureWatchPolicyError(
            f"password reference in {source.password_ref_env} did not resolve"
        )
    return with_password(dsn, password)


class PostgresCaptureWatchReader:
    """Read both aggregates using one read-only connection; reconnect on error."""

    def __init__(self, *, dsn: str) -> None:
        if not dsn.strip():
            raise CaptureWatchPolicyError("capture watch reads DSN is empty")
        self._dsn = dsn
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and getattr(self._conn, "closed", 0) == 0:
            return self._conn
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(self._dsn)
        return self._conn

    def close(self) -> None:
        """Drop a failed connection before another source attempts a read."""
        if self._conn is not None:
            connection, self._conn = self._conn, None
            connection.close()

    def read(
        self, source: ModelCaptureSource, *, active_since: datetime
    ) -> ModelCaptureObservation:
        """Interpolate validated identifiers; bind both cutoff and actor pattern."""
        relation = validate_identifier(source.relation)
        timestamp = validate_identifier(source.timestamp_column)
        actor = validate_identifier(source.actor_column)
        params: tuple[object, ...] = (active_since,)
        if source.actor_pattern is not None:
            actor = f"substring({actor} from %s)"
            params = (source.actor_pattern, active_since, source.actor_pattern)
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT max({timestamp}) FROM {relation}")
                source_row = cursor.fetchone()
                cursor.execute(
                    f"SELECT {actor}, max({timestamp}) FROM {relation} "
                    f"WHERE {timestamp} > %s AND {actor} IS NOT NULL GROUP BY 1",
                    params,
                )
                actor_rows = cursor.fetchall()
            if source_row is None:
                raise ValueError("capture source aggregate returned no result row")
            return ModelCaptureObservation(
                source_newest_at=source_row[0],
                actors=tuple(
                    ModelActorNewest(actor=row[0], newest_at=row[1])
                    for row in actor_rows
                ),
            )
        except Exception:
            self.close()
            raise
