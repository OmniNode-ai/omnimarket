# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime binding of the delegation eval read sources (OMN-20577).

The label-record and eval-run nodes read ``delegation_events`` and
``delegation_eval_items`` through a reader bound to the command's tenant. The one
deployment fact that reader needs, the name of the environment variable holding the read
DSN, comes from the node overlay ``delegation_eval_sources`` (field ``read_dsn_env``)
under ``ONEX_SKILL_OVERLAY_ROOTS``; the DSN itself is read from that variable. There is no
packaged default: with either absent the binding refuses with an error that names how to
supply it, and never prints a value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    resolve_setting,
)
from omnimarket.projection.postgres_read_database import PostgresReadDatabaseAdapter
from omnimarket.protocols.protocol_tenant_row_query import ProtocolTenantRowQuery

OVERLAY_ID = "delegation_eval_sources"
READ_DSN_ENV_FIELD = "read_dsn_env"


def build_tenant_row_query(
    tenant_id: str, environ: Mapping[str, str] | None = None
) -> ProtocolTenantRowQuery:
    """Return a read-only reader bound to ``tenant_id`` over the DSN the overlay names."""
    env = os.environ if environ is None else environ
    dsn_env = resolve_setting(
        OVERLAY_ID,
        READ_DSN_ENV_FIELD,
        fields=frozenset({READ_DSN_ENV_FIELD}),
        environ=env,
    )
    dsn = env.get(dsn_env, "").strip()
    if not dsn:
        raise NodeOverlayError(
            f"{OVERLAY_ID}: the variable named by {READ_DSN_ENV_FIELD} is not set in the runtime environment"
        )
    return PostgresReadDatabaseAdapter(dsn, tenant_id=tenant_id)
