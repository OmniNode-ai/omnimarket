# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation domain plugin for kernel-level initialization.

PluginDelegation implements ProtocolDomainPlugin for the Delegation
domain, encapsulating all delegation-specific initialization that
wires the delegation orchestrator, routing reducer, and quality gate
reducer into the runtime kernel.

The plugin handles:
    - Handler instantiation (HandlerDelegationWorkflow,
      HandlerDelegationRouting, HandlerQualityGate)
    - Dispatcher wiring into MessageDispatchEngine
    - Event consumer startup via EventBusSubcontractWiring

Design:
    Unlike ServiceRegistration, this plugin has no PostgreSQL dependency.
    The delegation pipeline is stateless (in-memory FSM per correlation_id)
    and activates unconditionally.

Related:
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from omnibase_infra.runtime.contract_topic_router import (
    build_topic_router_from_contract,
)
from omnibase_infra.runtime.models.model_handshake_result import (
    ModelHandshakeResult,
)
from omnibase_infra.runtime.protocol_domain_plugin import (
    ModelDomainPluginConfig,
    ModelDomainPluginResult,
)
from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

if TYPE_CHECKING:
    from omnibase_infra.runtime.event_bus_subcontract_wiring import (
        EventBusSubcontractWiring,
    )

logger = logging.getLogger(__name__)

# Build topic router from contract published_events at module import time.
_CONTRACT_PATH = Path(__file__).parent / "contract.yaml"
try:
    _contract_raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
except (OSError, yaml.YAMLError) as _contract_exc:
    logging.getLogger(__name__).warning(
        "Failed to load delegation contract at %s: %s. Using empty topic router.",
        _CONTRACT_PATH,
        _contract_exc,
    )
    _contract_raw = {}
_CONTRACT_DATA: dict[str, object] = (
    _contract_raw if isinstance(_contract_raw, dict) else {}
)
_TOPIC_ROUTER: dict[str, str] = build_topic_router_from_contract(_CONTRACT_DATA)


class PluginDelegation:
    """Delegation domain plugin for kernel initialization.

    Wires the three delegation nodes (orchestrator, routing reducer,
    quality gate reducer) into the runtime kernel. Stateless — no
    external resources beyond the event bus.
    """

    def __init__(self) -> None:
        self._wiring: EventBusSubcontractWiring | None = None
        self._handler_wiring_succeeded: bool = False
        self._dispatcher_wiring_succeeded: bool = False

    @property
    def plugin_id(self) -> str:
        return "delegation"

    @property
    def display_name(self) -> str:
        return "Delegation"

    def should_activate(self, _config: ModelDomainPluginConfig) -> bool:
        """Delegation plugin activates unconditionally.

        No external resource dependencies — the pipeline is purely
        event-driven with in-memory FSM state.
        """
        return True

    async def initialize(  # NOSONAR S7503: protocol-required async (ProtocolDomainPlugin); no await needed in no-op implementation
        self,
        _config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """No-op initialization — delegation has no external resources."""
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Delegation plugin initialized (no resources required)",
            resources_created=[],
            duration_seconds=0.0,
        )

    async def validate_handshake(  # NOSONAR S7503: protocol-required async (ProtocolDomainPlugin); no await needed in no-op implementation
        self,
        _config: ModelDomainPluginConfig,
    ) -> ModelHandshakeResult:
        """No handshake checks required for delegation."""
        return ModelHandshakeResult.default_pass(self.plugin_id)

    async def wire_handlers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Register delegation handlers with the container."""
        from omnimarket.nodes.node_delegation_orchestrator.wiring import (
            wire_delegation_handlers,
        )

        start_time = time.time()
        try:
            result = await wire_delegation_handlers(config.container)
            duration = time.time() - start_time

            logger.info(
                "Delegation handlers wired (correlation_id=%s)",
                config.correlation_id,
                extra={"services": result["services"]},
            )

            self._handler_wiring_succeeded = True

            return ModelDomainPluginResult(
                plugin_id=self.plugin_id,
                success=True,
                message="Delegation handlers wired",
                services_registered=result["services"],
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(
                "Failed to wire delegation handlers: %s",
                sanitize_error_message(e),
                extra={"correlation_id": str(config.correlation_id)},
            )
            return ModelDomainPluginResult.failed(
                plugin_id=self.plugin_id,
                error_message=sanitize_error_message(e),
                duration_seconds=duration,
            )

    async def wire_dispatchers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Wire delegation dispatchers into the MessageDispatchEngine."""
        start_time = time.time()

        if config.container.service_registry is None:
            logger.warning(
                "DEGRADED_MODE: ServiceRegistry not available, skipping "
                "delegation dispatcher wiring (correlation_id=%s)",
                config.correlation_id,
            )
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="ServiceRegistry not available",
            )

        if config.dispatch_engine is None:
            logger.warning(
                "DEGRADED_MODE: dispatch_engine not available, skipping "
                "delegation dispatcher wiring (correlation_id=%s)",
                config.correlation_id,
            )
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="dispatch_engine not available",
            )

        try:
            from omnimarket.nodes.node_delegation_orchestrator.wiring import (
                wire_delegation_dispatchers,
            )

            dispatch_summary = await wire_delegation_dispatchers(
                container=config.container,
                engine=config.dispatch_engine,
                correlation_id=config.correlation_id,
                event_bus=config.event_bus,
            )

            duration = time.time() - start_time
            logger.info(
                "Delegation dispatchers wired into engine (correlation_id=%s)",
                config.correlation_id,
                extra={
                    "dispatchers": dispatch_summary.get("dispatchers", []),
                    "routes": dispatch_summary.get("routes", []),
                },
            )

            self._dispatcher_wiring_succeeded = True
            return ModelDomainPluginResult(
                plugin_id=self.plugin_id,
                success=True,
                message="Delegation dispatchers wired into engine",
                resources_created=list(dispatch_summary.get("dispatchers", [])),
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(
                "Failed to wire delegation dispatchers: %s",
                sanitize_error_message(e),
                extra={"correlation_id": str(config.correlation_id)},
            )
            return ModelDomainPluginResult.failed(
                plugin_id=self.plugin_id,
                error_message=sanitize_error_message(e),
                duration_seconds=duration,
            )

    async def start_consumers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Start event consumers via EventBusSubcontractWiring."""
        start_time = time.time()
        correlation_id = config.correlation_id

        if not (self._handler_wiring_succeeded and self._dispatcher_wiring_succeeded):
            logger.warning(
                "Skipping consumer startup: handler/dispatcher wiring did not succeed "
                "for plugin '%s' (correlation_id=%s)",
                self.plugin_id,
                correlation_id,
            )
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="Handler/dispatcher wiring did not succeed — consumers not started",
            )

        if config.dispatch_engine is None:
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="dispatch_engine not available",
            )

        # Resolve the subscribe-capable event bus from the container (OMN-9556).
        # The kernel registers the shared runtime bus as ProtocolEventBusSubscriber
        # during step 4.1 so domain plugins resolve it here rather than reading
        # config.event_bus directly. This makes DI boundaries explicit and keeps
        # publisher-only paths unaffected.
        from omnibase_core.protocols.event_bus.protocol_event_bus_subscriber import (
            ProtocolEventBusSubscriber,
        )

        subscriber_bus: ProtocolEventBusSubscriber | None = None
        if config.container.service_registry is not None:
            with contextlib.suppress(Exception):
                subscriber_bus = (
                    await config.container.service_registry.resolve_service(
                        ProtocolEventBusSubscriber
                    )
                )

        # Fallback: if container resolution failed (e.g. test environments that
        # skip kernel registration), check config.event_bus directly.
        if subscriber_bus is None:
            if not isinstance(config.event_bus, ProtocolEventBusSubscriber):
                return ModelDomainPluginResult.skipped(
                    plugin_id=self.plugin_id,
                    reason="Event bus does not support subscribe (not in container and config.event_bus lacks subscribe capability)",
                )
            subscriber_bus = config.event_bus

        if config.node_identity is None:
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="node_identity not set (required for consumer subscription)",
            )

        wiring = None
        try:
            from omnibase_core.enums import EnumInjectionScope
            from omnibase_infra.runtime.event_bus_subcontract_wiring import (
                EventBusSubcontractWiring,
                load_event_bus_subcontract,
                load_published_events_map,
            )
            from omnibase_infra.runtime.service_dispatch_result_applier import (
                DispatchResultApplier,
            )

            contract_path = Path(__file__).parent / "contract.yaml"
            subcontract = load_event_bus_subcontract(contract_path, logger=logger)

            if subcontract is None:
                return ModelDomainPluginResult.skipped(
                    plugin_id=self.plugin_id,
                    reason=f"No event_bus subcontract in {contract_path}",
                )

            published_events_map = load_published_events_map(
                contract_path, logger=logger
            )

            logger.info(
                "Loaded published_events_map from %s: %d event-type->topic mappings",
                contract_path,
                len(published_events_map),
            )

            # DispatchResultApplier uses config.event_bus for publish_envelope
            # (publisher-only path, compatible with ProtocolEventBusPublisher).
            result_applier = DispatchResultApplier(
                event_bus=config.event_bus,
                output_topic=config.output_topic,
                topic_router=_TOPIC_ROUTER,
                output_topic_map=published_events_map,
            )

            if config.container.service_registry is not None:
                await config.container.service_registry.register_instance(
                    interface=DispatchResultApplier,
                    instance=result_applier,
                    scope=EnumInjectionScope.GLOBAL,
                    metadata={
                        "description": "Dispatch result applier for delegation domain",
                        "plugin_id": self.plugin_id,
                    },
                )

            # Use the container-resolved subscriber_bus for subscribe operations (OMN-9556).
            wiring = EventBusSubcontractWiring(
                event_bus=subscriber_bus,
                dispatch_engine=config.dispatch_engine,
                environment=config.node_identity.env,
                node_name=config.node_identity.node_name,
                service=config.node_identity.service,
                version=config.node_identity.version,
                result_applier=result_applier,
            )

            await wiring.wire_subscriptions(
                subcontract=subcontract,
                node_name="delegation-orchestrator",
            )

            from omnimarket.nodes.node_delegation_orchestrator.delegation_intent_bridge import (
                DelegationIntentBridge,
            )
            from omnimarket.nodes.node_delegation_orchestrator.wiring import (
                wire_delegation_bridge,
            )

            # Resolve the bridge registered by PluginLlm (has real LlmCallerDelegation).
            # Fall back to None so routing/quality-gate intents still work without LLM.
            llm_caller = None
            if config.container.service_registry is not None:
                try:
                    existing_bridge = (
                        await config.container.service_registry.resolve_service(
                            DelegationIntentBridge
                        )
                    )
                    llm_caller = existing_bridge.llm_caller
                    logger.info(
                        "PluginDelegation: resolved LlmCallerDelegation from container "
                        "(correlation_id=%s)",
                        correlation_id,
                    )
                except Exception:
                    logger.debug(
                        "PluginDelegation: DelegationIntentBridge not in container — "
                        "inference intents will be disabled (correlation_id=%s)",
                        correlation_id,
                    )

            bridge_result = await wire_delegation_bridge(
                event_bus=config.event_bus,
                llm_caller=llm_caller,
            )
            logger.info(
                "DelegationIntentBridge wired llm_caller=%s (correlation_id=%s): %s",
                type(llm_caller).__name__ if llm_caller else "None",
                correlation_id,
                bridge_result,
            )

            self._wiring = wiring

            logger.info(
                "Delegation consumers started via EventBusSubcontractWiring "
                "(correlation_id=%s)",
                correlation_id,
                extra={
                    "subscribe_topics": subcontract.subscribe_topics,
                    "topic_count": len(subcontract.subscribe_topics)
                    if subcontract.subscribe_topics
                    else 0,
                },
            )

            duration = time.time() - start_time

            async def _cleanup_wiring() -> None:
                if self._wiring is not None:
                    await self._wiring.cleanup()
                    self._wiring = None

            return ModelDomainPluginResult(
                plugin_id=self.plugin_id,
                success=True,
                message="Delegation consumers started via EventBusSubcontractWiring",
                duration_seconds=duration,
                unsubscribe_callbacks=[_cleanup_wiring],
            )

        except Exception as e:
            duration = time.time() - start_time
            if wiring is not None:
                await wiring.cleanup()
            self._wiring = None
            logger.exception(
                "Failed to start delegation consumers: %s",
                sanitize_error_message(e),
                extra={"correlation_id": str(correlation_id)},
            )
            return ModelDomainPluginResult.failed(
                plugin_id=self.plugin_id,
                error_message=sanitize_error_message(e),
                duration_seconds=duration,
            )

    async def shutdown(
        self,
        _config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Clean up delegation resources."""
        if self._wiring is not None:
            await self._wiring.cleanup()
            self._wiring = None
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Delegation plugin shut down",
            duration_seconds=0.0,
        )
