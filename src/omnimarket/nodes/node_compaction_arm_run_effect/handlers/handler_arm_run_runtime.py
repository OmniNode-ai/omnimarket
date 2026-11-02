# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime edge: resolve the declared dependencies through the composition root."""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.nodes.node_compaction_arm_run_effect.container.container_arm_run import (
    create_handler_compaction_arm_run,
)
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.arm_adapters import (
    ProtocolTerminalConsumer,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    ModelArmRunRequest,
    ModelArmRunResult,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_runtime_binding import (
    ModelArmRuntimeBinding,
)
from omnimarket.nodes.node_compaction_arm_run_effect.protocols.protocol_arm_adapter import (
    ProtocolArmAdapter,
)


class HandlerCompactionArmRunRuntime:
    def __init__(
        self,
        event_publisher: Callable[[str, bytes], None] | None = None,
        event_consumer: ProtocolTerminalConsumer | None = None,
        *,
        binding: ModelArmRuntimeBinding | None = None,
        stock: ProtocolArmAdapter | None = None,
        mod: ProtocolArmAdapter | None = None,
    ) -> None:
        self._handler = create_handler_compaction_arm_run(
            event_publisher,
            event_consumer,
            binding=binding,
            stock=stock,
            mod=mod,
        )

    def handle(self, request: ModelArmRunRequest) -> ModelArmRunResult:
        return self._handler.handle(request)
