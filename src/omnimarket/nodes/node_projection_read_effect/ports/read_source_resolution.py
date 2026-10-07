# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read node's database: the projection read binding, nothing else.

The database is the one the projection read binding names, resolved through
:func:`omnimarket.projection.runner.projection_read_binding_from_overlay_env`:
the read overlay's binding when the runtime carries one, otherwise the runtime's
projection binding overlay (``ModelProjectionRuntimeBinding``, the configuration
the projection writers are bound with). This module reads no environment
variable itself. ``node_delegate_skill_orchestrator``'s claim and evidence
stores follow the runtime binding only, so a read binding that logs in as a
reader never becomes their write principal (OMN-20159).

The one exception is the ``omninode_internal`` schema (OMN-20071): the read
binding's role holds no USAGE there, so those exposures are read through the
runtime's own principal, whose URL the runner resolves
(:func:`omnimarket.projection.runner.projection_internal_database_url`). The
URL is used only for a Postgres binding, and a runtime without it reads that
schema through the read binding as before.

The binding may name Postgres, where the deployed writers materialize their
tables, or the local SQLite store a local runtime's writers fill (local MVP
mode 1, OMN-20329). Either way the read goes to the database the writers were
bound to.

Unlike that node there is no fallback. A runtime with no binding has no
projection tables to read, and answering from some other database would
serve rows the writers never wrote, so the read is refused by name.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.runner import (
    PROJECTION_READ_BINDING_UNSET_DETAIL,
    ProjectionReadBindingOverlayError,
    projection_internal_database_url,
    projection_read_binding_from_overlay_env,
)
from omnimarket.projection.sqlite_database import (
    SQLITE_SCHEMES,
    sqlite_path_from_dsn,
)
from omnimarket.projection.table_reader import (
    ProjectionReadError,
    TableRowSource,
)

_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})


def resolve_projection_read_source() -> TableRowSource | SqliteTableRowSource:
    """A row source over the database the projection read binding names.

    Raises :class:`ProjectionReadError` ``projection_binding_unconfigured`` when
    the runtime carries neither a read nor a runtime binding,
    ``projection_binding_invalid`` when the read overlay is set but its file
    did not load (it never falls back to the runtime binding), and
    ``projection_binding_unsupported`` when the bound database is neither
    Postgres nor a SQLite file. With the read overlay unset, a runtime overlay
    that did not load raises its own load error, as it always has.
    """
    try:
        binding = projection_read_binding_from_overlay_env()
    except ProjectionReadBindingOverlayError as exc:
        # Its message names the variable, the file and the error's class only,
        # never the file's contents, so it is safe for an external caller.
        raise ProjectionReadError("projection_binding_invalid", str(exc)) from exc
    if binding is None:
        raise ProjectionReadError(
            "projection_binding_unconfigured",
            f"{PROJECTION_READ_BINDING_UNSET_DETAIL}, so this runtime has no "
            "projection tables to read",
        )
    # binding.source is where the binding came from (overlay:<path>), never a
    # credential, so the operator is sent to the file that was in effect.
    try:
        database_url = binding.resolve_database_url()
    except RuntimeError as exc:
        raise ProjectionReadError(
            "projection_binding_unconfigured",
            f"the projection read binding ({binding.source}) names a database "
            "reference that did not resolve",
        ) from exc
    scheme = urlsplit(database_url).scheme.lower()
    if scheme in _POSTGRES_SCHEMES:
        return TableRowSource.for_database_url(
            database_url, internal_database_url=projection_internal_database_url()
        )
    if scheme in SQLITE_SCHEMES:
        return SqliteTableRowSource(sqlite_path_from_dsn(database_url))
    raise ProjectionReadError(
        "projection_binding_unsupported",
        f"the projection read binding ({binding.source}) names a database that "
        "is neither Postgres nor a SQLite file",
    )


__all__ = ["resolve_projection_read_source"]
