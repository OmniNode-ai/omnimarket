# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind declared configuration and runtime-injected bus ports to arm adapters."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import yaml
from omnibase_core.models.context.model_context_access import ModelContextAccess

from omnimarket.nodes.node_compaction_arm_run_effect.handlers.arm_adapters import (
    HandlerModAdapter,
    HandlerNativeForkAdapter,
    HandlerPinnedPackAdapter,
    HandlerUnavailableArmAdapter,
    ProtocolTerminalConsumer,
)
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run import (
    HandlerCompactionArmRun,
)
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.workspace import (
    HandlerForkWorkspace,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_runtime_binding import (
    ModelArmRuntimeBinding,
)
from omnimarket.nodes.node_compaction_arm_run_effect.protocols.protocol_arm_adapter import (
    ProtocolArmAdapter,
)


def load_binding() -> ModelArmRuntimeBinding:
    path = Path(__file__).resolve().parents[1] / "contract.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))["arm_runtime"]
    root = os.environ.get(data["state_root_env"])
    if not root:
        raise ValueError("arm runtime state root is not configured")
    return ModelArmRuntimeBinding(
        workspace_root=Path(root) / data["workspace_subdirectory"],
        native_timeout_s=data["native_timeout_s"],
        pack_timeout_s=data["pack_timeout_s"],
        profile_name=data["profile_name"],
        tenant_id=os.environ.get(data["tenant_id_env"]),
        pack_requested_topic=data["pack_requested_topic"],
        pack_built_topic=data["pack_built_topic"],
        pack_failed_topic=data["pack_failed_topic"],
        native_requested_topic=data["native_requested_topic"],
        native_completed_topic=data["native_completed_topic"],
    )


def create_handler_compaction_arm_run(
    event_publisher: Callable[[str, bytes], None] | None = None,
    event_consumer: ProtocolTerminalConsumer | None = None,
    *,
    binding: ModelArmRuntimeBinding | None = None,
    stock: ProtocolArmAdapter | None = None,
    mod: ProtocolArmAdapter | None = None,
) -> HandlerCompactionArmRun:
    binding = binding if binding is not None else load_binding()
    native: ProtocolArmAdapter
    if stock is not None:
        native = stock
    elif event_publisher is not None and event_consumer is not None:
        native = HandlerNativeForkAdapter(
            event_publisher,
            event_consumer,
            requested_topic=binding.native_requested_topic,
            completed_topic=binding.native_completed_topic,
            timeout_s=binding.native_timeout_s,
        )
    else:
        native = HandlerUnavailableArmAdapter()
    if mod is None:
        if (
            event_publisher is not None
            and event_consumer is not None
            and binding.tenant_id
        ):
            mod = HandlerModAdapter(
                HandlerPinnedPackAdapter(
                    event_publisher,
                    event_consumer,
                    access=ModelContextAccess(tenant_id=binding.tenant_id),
                    profile_name=binding.profile_name,
                    requested_topic=binding.pack_requested_topic,
                    built_topic=binding.pack_built_topic,
                    failed_topic=binding.pack_failed_topic,
                    timeout_s=binding.pack_timeout_s,
                ),
                native,
            )
        else:
            mod = HandlerUnavailableArmAdapter()
    return HandlerCompactionArmRun(
        workspace=HandlerForkWorkspace(binding.workspace_root), stock=native, mod=mod
    )
