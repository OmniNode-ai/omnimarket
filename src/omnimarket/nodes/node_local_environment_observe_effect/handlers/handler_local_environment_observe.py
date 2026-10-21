# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tick-clocked, catalog-driven compose service observations."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.docker.catalog.manifest_schema import CatalogManifest
from omnibase_infra.docker.catalog.resolver import CatalogResolver
from omnibase_infra.enums import EnumHandlerType, EnumHandlerTypeCategory
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.local_deployment.tenant_identity import (
    EnumLocalTenantIdentityRefusalReason,
    LocalTenantIdentityError,
    ModelLocalTenantIdentityRefusal,
    resolve_local_deployment_tenant_id,
)
from omnimarket.models.model_local_environment_status import ModelLocalEnvironmentStatus
from omnimarket.nodes.contract_topics import contract_publish_topics

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_HANDLER_ID = "local-environment-observe-handler"


class ModelObserveSchedule(BaseModel):
    """Minimum elapsed tick time between observation passes."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    run_interval_seconds: float = Field(default=15, gt=0)


class ModelLocalEnvironmentObserveConfig(BaseModel):
    """Configuration loaded from the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    catalog_dir: str = "/app/docker/catalog"
    bundle: str = Field(default="local", min_length=1)
    probe_timeout_seconds: float = Field(default=3, gt=0)
    schedule: ModelObserveSchedule = Field(default_factory=ModelObserveSchedule)


class ModelProbeTarget(BaseModel):
    """Service-network destination derived from a catalog manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    service: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    scheme: Literal["http", "https"] | None = None
    path: str = "/"
    query: str = ""


class ModelProbeResult(BaseModel):
    """One probe's observation and human-readable evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observed_state: EnumLocalServiceState
    reason: str = Field(min_length=1)


class ProtocolServiceProber(Protocol):
    """Network boundary replaceable by an in-memory prober."""

    async def probe(self, target: ModelProbeTarget, timeout: float) -> ModelProbeResult:
        """Probe a catalog destination within the supplied timeout."""


class RealNetworkProber:
    """HTTP health reads and TCP connections, with no Docker dependency."""

    async def probe(self, target: ModelProbeTarget, timeout: float) -> ModelProbeResult:
        """Classify an HTTP response or the ability to connect to a TCP port."""
        destination = f"{target.service}:{target.port}"
        if target.scheme is None:
            try:
                async with asyncio.timeout(timeout):
                    _, writer = await asyncio.open_connection(
                        target.service, target.port
                    )
                    writer.close()
                    await writer.wait_closed()
                return ModelProbeResult(
                    observed_state=EnumLocalServiceState.RUNNING,
                    reason=f"TCP connection to {destination} succeeded.",
                )
            except (OSError, TimeoutError) as exc:
                return ModelProbeResult(
                    observed_state=EnumLocalServiceState.DOWN,
                    reason=f"TCP connection to {destination} failed ({type(exc).__name__}).",
                )

        url = urlunsplit((target.scheme, destination, target.path, target.query, ""))
        reason_url = urlunsplit((target.scheme, destination, target.path, "", ""))
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    timeout=timeout, trust_env=False
                ) as client:
                    response = await client.get(url)
            state = EnumLocalServiceState.RUNNING
            detail = ""
            if not 200 <= response.status_code < 300:
                state = EnumLocalServiceState.UNHEALTHY
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and str(body.get("status", "")).lower() in {
                    "degraded",
                    "unhealthy",
                    "down",
                    "error",
                    "failed",
                }:
                    state = EnumLocalServiceState.UNHEALTHY
                    detail = f"; JSON status reported {body['status']}"
            return ModelProbeResult(
                observed_state=state,
                reason=f"GET {reason_url} returned HTTP {response.status_code}{detail}.",
            )
        except (httpx.RequestError, OSError, TimeoutError) as exc:
            return ModelProbeResult(
                observed_state=EnumLocalServiceState.DOWN,
                reason=f"GET {reason_url} failed ({type(exc).__name__}).",
            )


def _load_contract_config() -> ModelLocalEnvironmentObserveConfig:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return ModelLocalEnvironmentObserveConfig.model_validate(
        raw["config"]["local_environment_observe"]
    )


def _probe_target(manifest: CatalogManifest) -> ModelProbeTarget | None:
    """Use a healthcheck HTTP port/path when present, else the internal TCP port."""
    health = manifest.healthcheck
    if health is not None:
        command = health.test if isinstance(health.test, str) else " ".join(health.test)
        for match in re.finditer(r"https?://[^\s'\"<>\)]+", command):
            parsed = urlsplit(match.group())
            if parsed.scheme in ("http", "https") and parsed.hostname:
                return ModelProbeTarget(
                    service=manifest.name,
                    port=parsed.port or (443 if parsed.scheme == "https" else 80),
                    scheme="https" if parsed.scheme == "https" else "http",
                    path=parsed.path or "/",
                    query=parsed.query,
                )
    if manifest.ports is None:
        return None
    return ModelProbeTarget(service=manifest.name, port=manifest.ports.internal)


class HandlerLocalEnvironmentObserve:
    """Return observations for the runtime to publish on the contract terminal."""

    def __init__(
        self,
        prober: ProtocolServiceProber | None = None,
        config: ModelLocalEnvironmentObserveConfig | None = None,
    ) -> None:
        self._prober = prober if prober is not None else RealNetworkProber()
        self._config = config or _load_contract_config()
        self._publish_topic = contract_publish_topics(_CONTRACT_PATH)[0]
        self._last_run: datetime | None = None

    @property
    def handler_type(self) -> EnumHandlerType:
        return EnumHandlerType.NODE_HANDLER

    @property
    def handler_category(self) -> EnumHandlerTypeCategory:
        return EnumHandlerTypeCategory.EFFECT

    async def handle(self, request: ModelRuntimeTick) -> ModelHandlerOutput[None]:
        """Gate using tick time; resolve identity and roster before concurrent I/O."""
        now = request.now.astimezone(UTC)
        interval = timedelta(seconds=self._config.schedule.run_interval_seconds)
        if self._last_run is not None and now - self._last_run < interval:
            return self._output(request, (), ())
        self._last_run = now
        try:
            tenant = resolve_local_deployment_tenant_id(None)
            if str(UUID(tenant)) != tenant:
                raise ValueError("Tenant is not a canonical UUID")
        except (LocalTenantIdentityError, ValueError) as exc:
            refusal = (
                exc.refusal
                if isinstance(exc, LocalTenantIdentityError)
                else ModelLocalTenantIdentityRefusal(
                    reason=EnumLocalTenantIdentityRefusalReason.IDENTITY_MALFORMED,
                    detail="The resolved tenant is not a canonical UUID.",
                )
            )
            logger.warning(
                "Local environment observation refused: %s", refusal.model_dump_json()
            )
            return self._output(request, (), (refusal.model_dump_json(),))

        stack = await asyncio.to_thread(
            lambda: CatalogResolver(self._config.catalog_dir).resolve(
                [self._config.bundle]
            )
        )
        targets: list[ModelProbeTarget] = []
        logs: list[str] = []
        for name, manifest in sorted(stack.manifests.items()):
            if manifest.restart == "no":
                reason = f"Excluded {name}: restart is 'no' (one-shot)."
            else:
                target = _probe_target(manifest)
                if target is not None:
                    targets.append(target)
                    continue
                reason = f"Excluded {name}: no port to probe."
            logger.info("%s", reason)
            logs.append(reason)
        results = await asyncio.gather(
            *(
                self._prober.probe(t, self._config.probe_timeout_seconds)
                for t in targets
            )
        )
        events = tuple(
            ModelLocalEnvironmentStatus(
                tenant_id=tenant,
                mode=EnumLocalEnvironmentMode.COMPOSE,
                service=target.service,
                observed_state=result.observed_state,
                reason=result.reason,
                observed_at=now,
                versions={},
            )
            for target, result in zip(targets, results, strict=True)
        )
        logger.debug(
            "Returning %d observations for %s", len(events), self._publish_topic
        )
        return self._output(request, events, tuple(logs))

    @staticmethod
    def _output(
        request: ModelRuntimeTick,
        events: tuple[ModelLocalEnvironmentStatus, ...],
        logs: tuple[str, ...],
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_effect(
            input_envelope_id=request.tick_id,
            correlation_id=request.correlation_id or uuid4(),
            handler_id=_HANDLER_ID,
            events=events,
            logs=logs,
        )
