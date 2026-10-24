# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the compaction runtime ports from its contract, lazily (OMN-21334)."""

from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import yaml
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.session_stream_barrier import HandlerSessionStreamBarrier
from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    ModelContextCompactionBuildOutcome,
    ModelContextCompactionRequested,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.session_barrier_probe import (
    ModelSessionBarrierReadBinding,
    PostgresSessionBarrierProbeSource,
)
from omnimarket.protocols.protocol_context_compaction_barrier_wait import (
    ProtocolContextCompactionBarrierWait,
)
from omnimarket.protocols.protocol_context_compaction_pack_build import (
    ProtocolContextCompactionPackBuild,
)
from omnimarket.protocols.protocol_session_barrier_probe_source import (
    ProtocolSessionBarrierProbeSource,
)

from .handler_compaction_pack_build import (
    HandlerCompactionPackBuild,
    ProtocolContextPackBuildCall,
)
from .handler_session_barrier_wait import HandlerSessionBarrierWait


class ModelCompactionRuntimeBinding(BaseModel):
    """The contract's pack build and barrier wait settings (OMN-21334)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack_build_node: str = Field(min_length=1)
    tenant_id: str | None
    barrier_deadline_s: float = Field(gt=0, allow_inf_nan=False)
    barrier_poll_interval_s: float = Field(gt=0, allow_inf_nan=False)
    barrier_probe: ModelSessionBarrierReadBinding


def load_compaction_runtime_binding(
    contract_path: Path | None = None,
) -> ModelCompactionRuntimeBinding:
    """Read the runtime binding and expand its tenant overlay (OMN-21334)."""
    path = (
        contract_path
        if contract_path is not None
        else Path(__file__).resolve().parents[1] / "contract.yaml"
    )
    try:
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("Malformed compaction runtime contract") from exc
    block = (
        contract.get("compaction_runtime") if isinstance(contract, Mapping) else None
    )
    if not isinstance(block, Mapping) or set(block) != {
        "pack_build_node",
        "tenant_id",
        "barrier_wait",
    }:
        raise ValueError(
            "compaction_runtime must declare pack_build_node, tenant_id and barrier_wait"
        )
    barrier = block["barrier_wait"]
    if not isinstance(barrier, Mapping) or set(barrier) != {
        "deadline_s",
        "poll_interval_s",
        "probe",
    }:
        raise ValueError(
            "compaction_runtime.barrier_wait must declare deadline_s, poll_interval_s and probe"
        )
    tenant = block["tenant_id"]
    if not isinstance(tenant, str):
        raise ValueError("compaction_runtime.tenant_id must be a string")
    tenant = expand_contract_env_refs(tenant).strip()
    return ModelCompactionRuntimeBinding.model_validate(
        {
            "pack_build_node": block["pack_build_node"],
            "tenant_id": tenant or None,
            "barrier_deadline_s": barrier["deadline_s"],
            "barrier_poll_interval_s": barrier["poll_interval_s"],
            "barrier_probe": barrier["probe"],
        }
    )


def resolve_pack_orchestrator(node_name: str) -> ProtocolContextPackBuildCall:
    """Resolve a no-argument handler through its own node contract (OMN-21334)."""
    package = f"omnimarket.nodes.{node_name}"
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError) as exc:
        raise ValueError(f"Cannot find pack build node {node_name!r}") from exc
    if spec is None:
        raise ValueError(f"Cannot find pack build node {node_name!r}")
    try:
        contract = yaml.safe_load(
            importlib.resources.files(package)
            .joinpath("contract.yaml")
            .read_text(encoding="utf-8")
        )
    except (OSError, TypeError, yaml.YAMLError) as exc:
        raise ValueError(
            f"Cannot read contract for pack build node {node_name!r}"
        ) from exc
    handler = contract.get("handler") if isinstance(contract, Mapping) else None
    if not isinstance(handler, Mapping):
        raise ValueError(f"Pack build node {node_name!r} must declare a handler")
    module_name = handler.get("module")
    class_name = handler.get("class")
    if not isinstance(module_name, str) or not isinstance(class_name, str):
        raise ValueError(
            f"Pack build node {node_name!r} must declare handler module and class"
        )
    handler_class = getattr(importlib.import_module(module_name), class_name)
    instance = handler_class()
    if not callable(getattr(instance, "handle", None)):
        raise ValueError(
            f"Pack build node {node_name!r} handler must have a callable handle"
        )
    return cast("ProtocolContextPackBuildCall", instance)


class LazyContractPackBuild:
    """Resolve the pack handler once, on the first build (OMN-21334)."""

    def __init__(
        self,
        node_name: str,
        access: ModelContextAccess,
        resolve: Callable[[str], ProtocolContextPackBuildCall] | None = None,
        probe_source: ProtocolSessionBarrierProbeSource | None = None,
    ) -> None:
        self._node_name = node_name
        self._access = access
        self._resolve = resolve if resolve is not None else resolve_pack_orchestrator
        self._pack_build: HandlerCompactionPackBuild | None = None
        self._probe_source = probe_source
        self._build_lock = threading.Lock()

    def build(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionBuildOutcome:
        revision = None
        if self._probe_source is not None:
            if request.barrier_id is None:
                return ModelContextCompactionBuildOutcome(
                    failure_class=EnumContextCompactionFailureClass.SOURCE_INCOMPLETE
                )
            probe = self._probe_source.read(request.session_id, request.barrier_id)
            if HandlerSessionStreamBarrier().handle(probe).outcome != "met":
                return ModelContextCompactionBuildOutcome(
                    failure_class=EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
                )
            revision = probe.source_revision
            if revision is None:
                return ModelContextCompactionBuildOutcome(
                    failure_class=EnumContextCompactionFailureClass.SOURCE_INCOMPLETE
                )
        # Resolved sources may keep synchronous read connections. Do not share
        # one connection concurrently between the async entrypoint's workers.
        with self._build_lock:
            if self._pack_build is None:
                self._pack_build = HandlerCompactionPackBuild(
                    self._resolve(self._node_name), access=self._access
                )
            return self._pack_build.build(request, source_revision=revision)


def bind_compaction_ports(
    binding: ModelCompactionRuntimeBinding | None = None,
    *,
    barrier_probe_source: ProtocolSessionBarrierProbeSource | None = None,
    resolve: Callable[[str], ProtocolContextPackBuildCall] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[
    ProtocolContextCompactionPackBuild | None,
    ProtocolContextCompactionBarrierWait | None,
]:
    """Bind both live ports; a missing projection never bypasses the barrier."""
    if binding is None:
        binding = load_compaction_runtime_binding()
    source = (
        barrier_probe_source
        if barrier_probe_source is not None
        else PostgresSessionBarrierProbeSource(binding.barrier_probe)
    )
    pack_build = (
        LazyContractPackBuild(
            binding.pack_build_node,
            ModelContextAccess(tenant_id=binding.tenant_id),
            resolve=resolve,
            probe_source=source,
        )
        if binding.tenant_id is not None
        else None
    )
    barrier_wait = HandlerSessionBarrierWait(
        source,
        deadline_s=binding.barrier_deadline_s,
        poll_interval_s=binding.barrier_poll_interval_s,
        monotonic=monotonic,
        sleep=sleep,
    )
    return pack_build, barrier_wait


__all__ = [
    "LazyContractPackBuild",
    "ModelCompactionRuntimeBinding",
    "bind_compaction_ports",
    "load_compaction_runtime_binding",
    "resolve_pack_orchestrator",
]
