# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeVersionSkewDetector — Detect plugin/runtime version incompatibility.

Pure compute handler. Compares plugin version against runtime version and checks
each installed node against a runtime compatibility range. Emits structured
output indicating healthy or skew_detected status.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)


def _load_publish_topics() -> list[str]:
    contract_path = Path(__file__).parent.parent / "contract.yaml"
    with open(contract_path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data.get("event_bus", {}).get("publish_topics", [])


class NodeVersionInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str


class IncompatibleNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    node_version: str
    reason: str


class VersionSkewCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin_version: str
    runtime_version: str
    installed_nodes: list[NodeVersionInfo]
    runtime_compat_range: str = ">=1.0.0,<2.0.0"


class VersionSkewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    plugin_version: str
    runtime_version: str
    incompatible_nodes: list[IncompatibleNode] = Field(default_factory=list)
    detected_at: str

    @property
    def has_skew(self) -> bool:
        return self.status == "skew_detected"


def _parse_version(version_str: str) -> Version:
    return Version(version_str)


def _check_semver_range(version_str: str, range_spec: str) -> bool:
    constraints = [c.strip() for c in range_spec.split(",")]
    ver = _parse_version(version_str)
    for constraint in constraints:
        constraint = constraint.strip()
        if constraint.startswith(">="):
            if ver < _parse_version(constraint[2:]):
                return False
        elif constraint.startswith(">"):
            if ver <= _parse_version(constraint[1:]):
                return False
        elif constraint.startswith("<="):
            if ver > _parse_version(constraint[2:]):
                return False
        elif constraint.startswith("<"):
            if ver >= _parse_version(constraint[1:]):
                return False
        elif constraint.startswith("=="):
            if ver != _parse_version(constraint[2:]):
                return False
        elif constraint.startswith("!=") and ver == _parse_version(constraint[2:]):
            return False
    return True


class NodeVersionSkewDetector:
    """Detect plugin/runtime version incompatibility across the platform.

    Pure compute handler — no I/O beyond reading the node's own contract.
    Accepts an optional event_bus for emitting version skew telemetry.
    """

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._publish_topics = _load_publish_topics()

    def handle(self, request: VersionSkewCheckRequest) -> VersionSkewResult:
        incompatible: list[IncompatibleNode] = []
        now = datetime.now(UTC).isoformat()

        plugin_ver: Version | None = None
        runtime_ver: Version | None = None
        try:
            plugin_ver = _parse_version(request.plugin_version)
        except Exception:
            incompatible.append(
                IncompatibleNode(
                    name="plugin",
                    node_version=request.plugin_version,
                    reason=f"Invalid plugin version: {request.plugin_version}",
                )
            )
        try:
            runtime_ver = _parse_version(request.runtime_version)
        except Exception:
            incompatible.append(
                IncompatibleNode(
                    name="runtime",
                    node_version=request.runtime_version,
                    reason=f"Invalid runtime version: {request.runtime_version}",
                )
            )

        if (
            plugin_ver is not None
            and runtime_ver is not None
            and plugin_ver.major != runtime_ver.major
        ):
            incompatible.append(
                IncompatibleNode(
                    name="plugin",
                    node_version=request.plugin_version,
                    reason=(
                        f"Major version mismatch: plugin {request.plugin_version} "
                        f"vs runtime {request.runtime_version}"
                    ),
                )
            )

        for node_info in request.installed_nodes:
            try:
                in_range = _check_semver_range(
                    node_info.version, request.runtime_compat_range
                )
                if not in_range:
                    incompatible.append(
                        IncompatibleNode(
                            name=node_info.name,
                            node_version=node_info.version,
                            reason=(
                                f"Node {node_info.name} version {node_info.version} "
                                f"outside runtime compatibility range "
                                f"{request.runtime_compat_range}"
                            ),
                        )
                    )
            except Exception:
                incompatible.append(
                    IncompatibleNode(
                        name=node_info.name,
                        node_version=node_info.version,
                        reason=f"Invalid version for node {node_info.name}: {node_info.version}",
                    )
                )

        status = "skew_detected" if incompatible else "healthy"
        self._last_result = VersionSkewResult(
            status=status,
            plugin_version=request.plugin_version,
            runtime_version=request.runtime_version,
            incompatible_nodes=incompatible,
            detected_at=now,
        )
        return self._last_result

    async def emit_skew_event(self, correlation_id: str) -> None:
        if self._event_bus is None or not self._publish_topics:
            return
        result = getattr(self, "_last_result", None)
        if result is None:
            return

        if result.status == "skew_detected":
            topic = next(
                (t for t in self._publish_topics if "version-skew-detected" in t),
                self._publish_topics[0],
            )
        else:
            topic = next(
                (t for t in self._publish_topics if "version-skew-healthy" in t),
                self._publish_topics[0],
            )

        payload = result.model_dump(mode="json")
        await self._event_bus.publish(
            topic=topic,
            key=correlation_id.encode(),
            value=json.dumps(payload, default=str).encode(),
        )
