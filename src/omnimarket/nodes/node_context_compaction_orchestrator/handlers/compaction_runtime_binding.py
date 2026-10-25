# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the compaction runtime ports from its contract, lazily (OMN-21334)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_compaction_orchestrator.handlers.session_barrier_probe import (
    ModelSessionBarrierReadBinding,
    PostgresSessionBarrierProbeSource,
)
from omnimarket.projection.postgres_read_database import (
    connect_read_only,
    read_sql_composer,
)
from omnimarket.protocols.protocol_context_compaction_barrier_wait import (
    ProtocolContextCompactionBarrierWait,
)
from omnimarket.protocols.protocol_session_barrier_probe_source import (
    ProtocolSessionBarrierProbeSource,
)

from .handler_session_barrier_wait import HandlerSessionBarrierWait


class ModelCompactionRuntimeBinding(BaseModel):
    """The contract's pack build and barrier wait settings (OMN-21334)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        "tenant_id",
        "barrier_wait",
    }:
        raise ValueError("compaction_runtime must declare tenant_id and barrier_wait")
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
            "tenant_id": tenant or None,
            "barrier_deadline_s": barrier["deadline_s"],
            "barrier_poll_interval_s": barrier["poll_interval_s"],
            "barrier_probe": barrier["probe"],
        }
    )


class ModelCompactionPackTopics(BaseModel):
    """The contract-declared pack request and terminal topics."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request_topic: str = Field(min_length=1)
    completed_topic: str = Field(min_length=1)
    failed_topic: str = Field(min_length=1)


def load_compaction_pack_topics(
    contract_path: Path | None = None,
) -> ModelCompactionPackTopics:
    path = contract_path or Path(__file__).resolve().parents[1] / "contract.yaml"
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    instances = contract["event_bus"]["request_response"]["instances"]
    matches = [
        instance
        for instance in instances
        if instance["name"] == "compaction_pack_build"
    ]
    if len(matches) != 1:
        raise ValueError(
            "Expected exactly one compaction_pack_build request_response instance"
        )
    instance = matches[0]
    return ModelCompactionPackTopics(
        request_topic=instance["request_topic"],
        completed_topic=instance["reply_topics"]["completed"],
        failed_topic=instance["reply_topics"]["failed"],
    )


def bind_compaction_ports(
    binding: ModelCompactionRuntimeBinding,
    *,
    barrier_probe_source: ProtocolSessionBarrierProbeSource | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ProtocolContextCompactionBarrierWait, ProtocolSessionBarrierProbeSource]:
    """Bind the existing barrier wait and its revision probe source."""
    source = (
        barrier_probe_source
        if barrier_probe_source is not None
        else PostgresSessionBarrierProbeSource(
            binding.barrier_probe,
            connect=connect_read_only,
            sql_composer=read_sql_composer,
        )
    )
    return HandlerSessionBarrierWait(
        source,
        deadline_s=binding.barrier_deadline_s,
        poll_interval_s=binding.barrier_poll_interval_s,
        monotonic=monotonic,
        sleep=sleep,
    ), source


__all__ = [
    "ModelCompactionPackTopics",
    "ModelCompactionRuntimeBinding",
    "bind_compaction_ports",
    "load_compaction_pack_topics",
    "load_compaction_runtime_binding",
]
