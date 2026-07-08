# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Config-driven resolution of the local delegation evidence DB target (OMN-14015).

The bus-less ``onex delegate`` CLI path (``LocalDelegationDispatchPort``) writes
its outcome-evidence row through the canonical projection handler into a
``ProtocolProjectionDatabaseSync`` backing store. Before OMN-14015 that store was
a *hardcoded* ``SqliteDatabaseAdapter(default_evidence_db_path())`` baked into the
port constructor — so CLI delegations could only ever land in a local SQLite
side-target, never on the platform Kafka->Postgres substrate the dashboards and
``context_roi_scores`` are built from. That hardcode is a doctrine violation:
infra targets (DB / projection destinations) must come from CONTRACT + overlay,
not a source-literal default, and it is the reason the OMN-14001 learning loop
cannot close its round-trip on the local path (it reads ROI from platform
Postgres but wrote evidence to local SQLite).

This module makes the evidence DB target a *configuration choice* resolved from
the SAME ``ModelProjectionRuntimeBinding`` overlay projection runners already use
(``OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY``):

* No overlay configured (the steady state for a truly bus-less local CLI) ->
  ``SqliteDatabaseAdapter(default_evidence_db_path())``. This preserves the prior
  default byte-for-byte, so golden replays are unaffected.
* Overlay declares a Postgres ``database_url`` (a deployed lane that co-locates
  the projection substrate) -> ``PostgresSyncProjectionAdapter`` targeting that
  DSN, so the CLI's evidence reaches the same durable substrate the bus runtime
  writes to — no code branch, purely by overlay.
* Overlay declares a ``sqlite:``/file ``database_url`` -> a
  ``SqliteDatabaseAdapter`` at that path (an explicit SQLite choice, still config,
  not a source literal).

Selecting SQLite for a bus-less CLI remains valid — the point is that it is now a
*config choice*, not a hardcode.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.protocol_database import DatabaseAdapter
from omnimarket.projection.runner import projection_runtime_binding_from_overlay_env
from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    default_evidence_db_path,
)

logger = logging.getLogger(__name__)

_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})
_SQLITE_SCHEMES = frozenset({"sqlite", "file"})


def _sqlite_path_from_dsn(dsn: str) -> Path:
    """Extract a filesystem path from a ``sqlite:``/``file:`` DSN or bare path.

    Follows the SQLAlchemy-style slash convention: ``sqlite:///rel/path`` is a
    relative path (``rel/path``) and ``sqlite:////abs/path`` is absolute
    (``/abs/path``) — i.e. exactly one leading slash from the URL path component
    is the scheme separator and is stripped.
    """
    split = urlsplit(dsn)
    if split.scheme in _SQLITE_SCHEMES:
        raw = split.path or split.netloc
        if raw.startswith("/"):
            raw = raw[1:]
        return Path(raw)
    return Path(dsn)


def _adapter_for_dsn(dsn: str) -> DatabaseAdapter:
    """Select the sync adapter whose backing store matches ``dsn``'s scheme."""
    scheme = urlsplit(dsn).scheme.lower()
    if scheme in _POSTGRES_SCHEMES:
        return PostgresSyncProjectionAdapter(dsn)
    if scheme in _SQLITE_SCHEMES or not scheme:
        return SqliteDatabaseAdapter(_sqlite_path_from_dsn(dsn))
    raise ValueError(
        f"unsupported delegation evidence database_url scheme {scheme!r}; "
        "use a postgres[ql]:// or sqlite:/file: DSN"
    )


def resolve_local_delegation_evidence_db() -> DatabaseAdapter:
    """Resolve the local delegation evidence DB adapter from config.

    Consults the projection runtime binding overlay (the SAME config surface the
    projection runners use, resolved by
    ``projection_runtime_binding_from_overlay_env`` — this module never reads the
    overlay env itself, keeping the env-read confined to the projection package
    per the delegation env-read discipline). When a binding is configured, its
    ``database_url`` selects the adapter (Postgres substrate or explicit SQLite).
    When none is configured, falls back to the canonical local SQLite evidence
    target — identical to the pre-OMN-14015 hardcoded default, so bus-less CLI
    behavior and golden replays are unchanged.
    """
    binding = projection_runtime_binding_from_overlay_env()
    if binding is None:
        return SqliteDatabaseAdapter(default_evidence_db_path())

    dsn = binding.resolve_database_url()
    adapter = _adapter_for_dsn(dsn)
    logger.info(
        "LocalDelegationDispatch evidence DB resolved from projection runtime "
        "binding (source=%s, adapter=%s)",
        binding.source,
        type(adapter).__name__,
    )
    return adapter


__all__ = ["resolve_local_delegation_evidence_db"]
