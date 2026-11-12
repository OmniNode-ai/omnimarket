# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build a tenant projection reader from a secret resolver the lane renders.

The bus handlers are built by the runtime with no constructor arguments. Their
Postgres reader needs the analytics DSN, which the lane carries as a rendered
secret-resolver config (``ONEX_SECRET_RESOLVER_CONFIG_PATH``). This module gives
a bus handler a way to obtain that reader: use the resolver the runtime hands it
when it has one, otherwise read the lane's rendered resolver config and build a
``SecretResolver`` from it. With neither, the reader is ``None`` and the handler
answers the typed ``not_configured`` terminal instead of guessing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from omnibase_infra.runtime.models.model_secret_resolver_config import (
    ModelSecretResolverConfig,
)
from omnibase_infra.runtime.runtime_profile import (
    resolve_secret_resolver_config_path,
)
from omnibase_infra.runtime.secret_resolver import SecretResolver

from omnimarket.handlers.container.container_overlay_dependencies import (
    packaged_contract_value,
)
from omnimarket.handlers.tenant_projection_read.analytics_postgres import (
    ProtocolLogicalSecretResolver,
    Psycopg2AnalyticsQueryExecutor,
    SecretResolverDsnSource,
)
from omnimarket.handlers.tenant_projection_read.projection_reader import (
    AnalyticsTenantProjectionReader,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProtocolTenantDelegationProjectionReader,
)

logger = logging.getLogger(__name__)


def runtime_secret_resolver(logical_name: str) -> SecretResolver | None:
    """The ``SecretResolver`` the lane's rendered config backs, or ``None``.

    The path is the runtime's rendered secret-resolver config (an env var this
    module does not read directly). An unreadable or malformed file, or a config
    that does not map ``logical_name``, yields ``None`` -- a warning is logged
    naming only the error type, never a secret value or the file contents.
    """
    path = resolve_secret_resolver_config_path()
    if not path:
        return None
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        config = ModelSecretResolverConfig.model_validate(raw)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("secret resolver config unreadable: %s", type(exc).__name__)
        return None
    if logical_name not in {m.logical_name for m in config.mappings}:
        return None
    return SecretResolver(config=config)


def reader_for_node(
    package: str,
    secret_resolver: ProtocolLogicalSecretResolver | None,
) -> ProtocolTenantDelegationProjectionReader | None:
    """A Postgres projection reader for ``package``, or ``None`` when no resolver.

    ``package`` is the node's import path; its packaged contract names the logical
    secret for the analytics DSN (``config.analytics_dsn_secret``). The resolver is
    the one the runtime hands the handler, or -- when it hands none -- the one read
    from the lane's rendered config. With no resolver the reader is ``None``.
    """
    secret = packaged_contract_value(package, "config", "analytics_dsn_secret")
    resolver = (
        secret_resolver
        if secret_resolver is not None
        else runtime_secret_resolver(str(secret))
    )
    if resolver is None:
        return None
    return AnalyticsTenantProjectionReader(
        Psycopg2AnalyticsQueryExecutor(SecretResolverDsnSource(resolver, str(secret)))
    )


__all__: list[str] = ["reader_for_node", "runtime_secret_resolver"]
