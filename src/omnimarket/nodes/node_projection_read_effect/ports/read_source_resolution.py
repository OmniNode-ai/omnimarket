# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read node's database: the runtime's projection binding, nothing else.

The database is the one the runtime's projection binding overlay names
(``ModelProjectionRuntimeBinding``, the configuration the projection writers
are bound with), resolved through
:func:`omnimarket.projection.runner.projection_runtime_binding_from_overlay_env`
-- the same resolver ``node_delegate_skill_orchestrator`` uses, so this module
reads no environment variable itself.

Unlike that node there is no fallback. A runtime with no binding has no
projection tables to read, and answering from some other database would
serve rows the writers never wrote, so the read is refused by name.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from omnimarket.projection.runner import projection_runtime_binding_from_overlay_env
from omnimarket.projection.table_reader import ProjectionReadError, TableRowSource

_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})


def resolve_projection_read_source() -> TableRowSource:
    """A row source over the database the runtime binding names.

    Raises :class:`ProjectionReadError` ``projection_binding_unconfigured`` when
    the runtime carries no binding, and ``projection_binding_unsupported`` when
    the bound database is not Postgres (the writer tables this node reads are
    materialized in Postgres).
    """
    binding = projection_runtime_binding_from_overlay_env()
    if binding is None:
        raise ProjectionReadError(
            "projection_binding_unconfigured",
            "this runtime carries no projection runtime binding, so it has no "
            "projection tables to read",
        )
    try:
        database_url = binding.resolve_database_url()
    except RuntimeError as exc:
        raise ProjectionReadError(
            "projection_binding_unconfigured",
            "the projection runtime binding's database reference did not resolve",
        ) from exc
    if urlsplit(database_url).scheme.lower() not in _POSTGRES_SCHEMES:
        raise ProjectionReadError(
            "projection_binding_unsupported",
            "the projection runtime binding names a database that is not Postgres",
        )
    return TableRowSource.for_database_url(database_url)


__all__ = ["resolve_projection_read_source"]
