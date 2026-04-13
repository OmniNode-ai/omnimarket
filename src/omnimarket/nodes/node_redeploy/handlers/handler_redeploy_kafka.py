# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRedeployKafka — real Kafka publish-monitor handler for node_redeploy.

Publishes a rebuild command to the deploy agent via Kafka, then polls the
completion topic until the matching correlation_id arrives or the timeout
elapses.

Topics (from contract.yaml):
  Publish:   onex.cmd.deploy.rebuild-requested.v1
  Subscribe: onex.evt.deploy.rebuild-completed.v1

The deploy agent on .201 consumes the command topic independently.
This handler never SSHes, never calls rpk directly, and has no subprocess
calls — pure event bus publish-monitor.

Degrades gracefully: if the event bus is unavailable at construction time,
execute() raises RuntimeError with a clear message rather than silently
succeeding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import uuid4

from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    EnumRedeployScope,
    EnumRedeployStatus,
    ModelDeployPhaseResults,
    ModelDeployRebuildCommand,
    ModelDeployRebuildCompleted,
    ModelRedeployResult,
)

TOPIC_DEPLOY_REBUILD_COMPLETED = "onex.evt.deploy.rebuild-completed.v1"
TOPIC_DEPLOY_REBUILD_REQUESTED = "onex.cmd.deploy.rebuild-requested.v1"

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 600
_POLL_INTERVAL_S = 2.0


class HandlerRedeployKafka:
    """Publish-monitor handler for deploy agent rebuilds.

    Accepts any event bus that satisfies ProtocolEventBus (duck-typed):
    EventBusInmemory for tests, EventBusKafka for production.

    Usage::

        handler = HandlerRedeployKafka(event_bus=bus)
        result = await handler.execute(
            scope="full",
            git_ref="origin/main",
            requested_by="node_redeploy",
        )
    """

    def __init__(
        self,
        event_bus: Any,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        if event_bus is None:
            raise RuntimeError(  # error-ok: mis-wired constructor
                "HandlerRedeployKafka requires an event_bus. "
                "Set KAFKA_BOOTSTRAP_SERVERS and wire EventBusKafka, "
                "or pass EventBusInmemory for testing."
            )
        self._bus: Any = event_bus
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    @property
    def bus(self) -> Any:
        """The underlying event bus instance."""
        return self._bus

    @property
    def timeout_s(self) -> float:
        """Timeout in seconds for completion polling."""
        return self._timeout_s

    @timeout_s.setter
    def timeout_s(self, value: float) -> None:
        self._timeout_s = value

    async def execute(
        self,
        scope: str = "full",
        git_ref: str = "origin/main",
        services: list[str] | None = None,
        requested_by: str = "node_redeploy",
        correlation_id: str | None = None,
    ) -> ModelRedeployResult:
        """Publish rebuild command and wait for completion event.

        Args:
            scope: Rebuild scope ("full", "runtime", "core").
            git_ref: Git ref for deploy agent to pull.
            services: Optional service filter. Empty = scope default.
            requested_by: Identity label emitted in the command.
            correlation_id: Override generated UUID (for testing).

        Returns:
            ModelRedeployResult with build duration, services, success/failure.

        Raises:
            RuntimeError: If event bus is not started.
        """
        corr_id = correlation_id or str(uuid4())

        try:
            scope_enum = EnumRedeployScope(scope)
        except ValueError:
            raise ValueError(  # error-ok: caller supplied invalid scope
                f"Unknown scope {scope!r}. Valid values: {[s.value for s in EnumRedeployScope]}"
            ) from None

        command = ModelDeployRebuildCommand(
            correlation_id=corr_id,
            requested_by=requested_by,
            scope=scope_enum,
            services=services or [],
            git_ref=git_ref,
        )

        completion_future: asyncio.Future[ModelDeployRebuildCompleted] = (
            asyncio.get_event_loop().create_future()
        )

        async def _on_completion(message: Any) -> None:
            if completion_future.done():
                return
            try:
                raw = message.value
                if isinstance(raw, bytes | bytearray):
                    payload = json.loads(raw.decode())
                elif isinstance(raw, str):
                    payload = json.loads(raw)
                else:
                    payload = raw

                event_corr_id = payload.get("correlation_id", "")
                if event_corr_id != corr_id:
                    return  # Different rebuild, ignore

                completed = ModelDeployRebuildCompleted(**payload)
                completion_future.set_result(completed)
            except Exception as exc:
                logger.warning(
                    "Failed to parse rebuild-completed event: %s", exc, exc_info=True
                )

        # Subscribe before publishing to avoid a race where the event arrives
        # before we're subscribed (only relevant for in-memory bus in tests).
        unsubscribe = await self._bus.subscribe(
            TOPIC_DEPLOY_REBUILD_COMPLETED,
            on_message=_on_completion,
            group_id=f"node-redeploy-{corr_id[:8]}",
        )

        cmd_payload = json.dumps(command.model_dump(mode="json")).encode()
        await self._bus.publish(
            TOPIC_DEPLOY_REBUILD_REQUESTED,
            key=corr_id.encode(),
            value=cmd_payload,
        )

        logger.info(
            "Redeploy command published",
            extra={
                "correlation_id": corr_id,
                "scope": scope,
                "git_ref": git_ref,
                "topic": TOPIC_DEPLOY_REBUILD_REQUESTED,
            },
        )

        start_time = time.monotonic()
        timed_out = False
        completed_event: ModelDeployRebuildCompleted | None = None

        try:
            completed_event = await asyncio.wait_for(
                completion_future,
                timeout=self._timeout_s,
            )
        except TimeoutError:
            timed_out = True
            logger.error(
                "Redeploy timed out after %ss waiting for correlation_id=%s",
                self._timeout_s,
                corr_id,
            )
        finally:
            await unsubscribe()

        elapsed = time.monotonic() - start_time

        if timed_out or completed_event is None:
            return ModelRedeployResult(
                correlation_id=corr_id,
                success=False,
                status=EnumRedeployStatus.FAILED,
                duration_seconds=elapsed,
                timed_out=True,
                errors=[
                    f"Timed out after {self._timeout_s}s waiting for deploy agent "
                    f"completion (correlation_id={corr_id})"
                ],
            )

        phase_results = {}
        if completed_event.phase_results:
            phase_results = {
                "git": completed_event.phase_results.git.value,
                "core": completed_event.phase_results.core.value,
                "runtime": completed_event.phase_results.runtime.value,
                "verification": completed_event.phase_results.verification.value,
                "publish": completed_event.phase_results.publish.value,
            }

        # Use the agent's reported duration if available, else wall-clock
        duration = (
            completed_event.duration_seconds
            if completed_event.duration_seconds > 0
            else elapsed
        )

        success = completed_event.status == EnumRedeployStatus.SUCCESS

        logger.info(
            "Redeploy completed",
            extra={
                "correlation_id": corr_id,
                "status": completed_event.status,
                "duration_seconds": duration,
                "git_sha": completed_event.git_sha,
                "services_restarted": completed_event.services_restarted,
            },
        )

        return ModelRedeployResult(
            correlation_id=corr_id,
            success=success,
            status=completed_event.status,
            duration_seconds=duration,
            git_sha=completed_event.git_sha,
            services_restarted=completed_event.services_restarted,
            phase_results=phase_results,
            errors=completed_event.errors,
            timed_out=False,
        )

    def handle(self, command: Any) -> dict[str, Any]:
        """Contract-driven entry point — bridges sync runtime to async execute().

        Accepts ModelDeployRebuildCommand or a plain dict; returns model_dump().
        """
        from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
            ModelDeployRebuildCommand,
        )

        if isinstance(command, dict):
            command = ModelDeployRebuildCommand(**command)
        result = asyncio.run(
            self.execute(
                scope=command.scope.value
                if hasattr(command.scope, "value")
                else str(command.scope),
                git_ref=command.git_ref,
                services=list(command.services) if command.services else None,
                requested_by=command.requested_by,
                correlation_id=command.correlation_id,
            )
        )
        return result.model_dump(mode="json")

    @classmethod
    def from_env(cls) -> HandlerRedeployKafka:
        """Construct handler from environment variables.

        Reads KAFKA_BOOTSTRAP_SERVERS. Raises RuntimeError if not set.

        Returns:
            HandlerRedeployKafka wired to EventBusKafka.
        """
        import os

        bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not bootstrap:
            raise RuntimeError(  # error-ok: environment mis-configuration
                "KAFKA_BOOTSTRAP_SERVERS is not set. "
                "Cannot construct HandlerRedeployKafka."
            )

        try:
            from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

            bus = EventBusKafka(
                bootstrap_servers=bootstrap,
                environment=os.environ.get("KAFKA_ENVIRONMENT", "production"),
                group="node-redeploy",
            )
        except ImportError as exc:
            raise RuntimeError(  # error-ok: missing optional dependency
                f"omnibase_infra not available: {exc}. "
                "Install omnibase_infra to use Kafka-backed redeployment."
            ) from exc

        return cls(event_bus=bus)

    @staticmethod
    def make_completion_event(
        correlation_id: str,
        status: str = "success",
        duration_seconds: float = 10.0,
        git_sha: str = "abc1234",
        services_restarted: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> ModelDeployRebuildCompleted:
        """Build a synthetic completion event (for testing / simulation)."""
        return ModelDeployRebuildCompleted(
            correlation_id=correlation_id,
            status=EnumRedeployStatus(status),
            duration_seconds=duration_seconds,
            git_sha=git_sha,
            services_restarted=services_restarted or [],
            phase_results=ModelDeployPhaseResults(),
            errors=errors or [],
        )


__all__: list[str] = ["HandlerRedeployKafka"]
