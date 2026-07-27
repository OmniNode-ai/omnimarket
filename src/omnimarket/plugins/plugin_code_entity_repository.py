# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Domain plugin that registers the code-entity repository provider (OMN-15230).

Why a domain plugin
-------------------
``node_code_embedding_effect`` and ``node_code_enrichment_effect`` resolve
``ProtocolCodeEntityRepository`` from the boot container at their effect
boundary (OMN-15228 / OMN-15229). Nothing ever put a provider *into* that
container, so both nodes were boot-resolvable and dispatch-non-functional.

The kernel's only in-repo registration seam is the ``onex.domain_plugins``
entry-point group: ``ServiceKernel`` step 4.6 runs
``RegistryDomainPlugin.discover_from_entry_points()`` over that group, gated by
a namespace allowlist that already contains ``omnimarket.``. Discovered plugins
get ``ModelDomainPluginConfig.container`` — the live boot container — passed to
``wire_handlers()``. Registering there needs no omnibase_infra change and no
direct infra -> omnimarket import (which the layering rule forbids).

``PluginCodeEntityRepository`` owns no daemon, service, worker, runtime,
consumer, publisher, client, controller or server lifecycle and subclasses
nothing, so it is outside the CLAUDE.md rule 7a / OMN-13284 ``Plugin*``
lifecycle ban (mechanically: the ``no_plugin_daemon_classes`` validator flags
``Plugin*`` classes whose own name or a base name contains a lifecycle term).
It registers one already-constructed instance and closes its pool on shutdown.

Activation posture
------------------
The plugin activates unconditionally and registers the adapter even when
``OMNIINTELLIGENCE_DB_URL`` is unset. That is deliberate: the adapter builds its
pool lazily, so an unset DSN raises a *configuration* error naming the env var
at the effect boundary, which is strictly more diagnosable than the *wiring*
error ("no provider registered") the nodes raise today. Conditioning activation
on the env var would silently reintroduce the exact gap this ticket closes.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from omnimarket.protocols.protocol_code_entity_repository import (
    ProtocolCodeEntityRepository,
)
from omnimarket.repositories.repository_code_entity_postgres import (
    RepositoryCodeEntityPostgres,
)

if TYPE_CHECKING:
    from omnibase_core.models.runtime.model_domain_plugin import (
        ModelDomainPluginConfig,
        ModelDomainPluginResult,
    )

logger = logging.getLogger(__name__)

PLUGIN_ID = "code_entity_repository"  # string-id-ok: plugin id, not a UUID


class PluginCodeEntityRepository:
    """Registers ``ProtocolCodeEntityRepository`` in the boot container.

    NOTE: consumed cross-repo by the omnibase_infra service kernel via the
    ``onex.domain_plugins`` entry point (declared in this repo's
    ``pyproject.toml``). Static analysis that scans only omnimarket will report
    zero importers — that is a false positive. Do not delete without removing
    the entry point.
    """

    def __init__(self) -> None:
        self._repository: RepositoryCodeEntityPostgres | None = None

    @property
    def plugin_id(self) -> str:
        return PLUGIN_ID

    @property
    def display_name(self) -> str:
        return "Code Entity Repository"

    def should_activate(self, _config: ModelDomainPluginConfig) -> bool:
        """Always activate — see "Activation posture" in the module docstring."""
        return True

    async def initialize(  # NOSONAR S7503: protocol-required async (ProtocolDomainPlugin); the pool is lazy, so nothing is awaited here
        self,
        _config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Construct the repository adapter. Opens no connection."""
        from omnibase_core.models.runtime.model_domain_plugin import (
            ModelDomainPluginResult,
        )

        self._repository = RepositoryCodeEntityPostgres()
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Code entity repository adapter constructed (pool is lazy)",
            resources_created=[type(self._repository).__name__],
        )

    async def wire_handlers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Register the adapter under ``ProtocolCodeEntityRepository``."""
        from omnibase_core.models.runtime.model_domain_plugin import (
            ModelDomainPluginResult,
        )

        start_time = time.time()

        if config.container.service_registry is None:
            logger.warning(
                "DEGRADED_MODE: ServiceRegistry unavailable, code entity "
                "repository not registered — node_code_embedding_effect and "
                "node_code_enrichment_effect will fail at dispatch "
                "(correlation_id=%s)",
                config.correlation_id,
            )
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="ServiceRegistry not available",
            )

        # initialize() runs before wire_handlers() in the kernel lifecycle, but
        # the plugin must not depend on that ordering to be correct.
        if self._repository is None:
            self._repository = RepositoryCodeEntityPostgres()

        try:
            await config.container.service_registry.register_instance(
                # A Protocol is the canonical DI key for get_service; it is
                # never instantiated. The container keys registrations on
                # `interface.__name__`, so this single registration serves both
                # consuming nodes.
                ProtocolCodeEntityRepository,  # type: ignore[type-abstract]  # Protocol used as DI key
                self._repository,
            )
        except Exception as exc:
            logger.exception(
                "Failed to register ProtocolCodeEntityRepository (correlation_id=%s)",
                config.correlation_id,
            )
            return ModelDomainPluginResult.failed(
                plugin_id=self.plugin_id,
                error_message=str(exc),
                duration_seconds=time.time() - start_time,
            )

        logger.info(
            "Registered %s as ProtocolCodeEntityRepository (correlation_id=%s)",
            type(self._repository).__name__,
            config.correlation_id,
        )
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="ProtocolCodeEntityRepository provider registered",
            services_registered=[ProtocolCodeEntityRepository.__name__],
            duration_seconds=time.time() - start_time,
        )

    async def wire_dispatchers(  # NOSONAR S7503: protocol-required async (ProtocolDomainPlugin); nothing to await
        self,
        _config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """No dispatchers — the two consuming nodes are contract-auto-wired."""
        from omnibase_core.models.runtime.model_domain_plugin import (
            ModelDomainPluginResult,
        )

        return ModelDomainPluginResult.skipped(
            plugin_id=self.plugin_id,
            reason="provider-only plugin; dispatch routes are contract-managed",
        )

    async def start_consumers(  # NOSONAR S7503: protocol-required async (ProtocolDomainPlugin); nothing to await
        self,
        _config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """No consumers — this plugin subscribes to nothing."""
        from omnibase_core.models.runtime.model_domain_plugin import (
            ModelDomainPluginResult,
        )

        return ModelDomainPluginResult.skipped(
            plugin_id=self.plugin_id,
            reason="provider-only plugin; no topic subscriptions",
        )

    async def shutdown(
        self,
        _config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Close the adapter's pool if one was opened."""
        from omnibase_core.models.runtime.model_domain_plugin import (
            ModelDomainPluginResult,
        )

        if self._repository is not None:
            await self._repository.close()
            self._repository = None
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Code entity repository pool closed",
        )


__all__ = ["PLUGIN_ID", "PluginCodeEntityRepository"]
