# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire the existing action classifier, model port and isolated scratch storage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omnibase_core.models.container.model_onex_container import ModelONEXContainer
from omnibase_core.utils.util_run_coro_sync import run_coro_sync

from omnimarket.nodes.node_compaction_action_score_compute import (
    HandlerCompactionActionScore,
)

from ..handlers.handler_continuation_replay import (
    HandlerContinuationReplay,
    StubFactory,
)
from ..handlers.handler_repeat_stability import HandlerRepeatStability
from ..handlers.policy import load_component_handler, load_continuation_policy
from ..handlers.stubs import SandboxTools
from ..models.model_policy import ModelContinuationPolicy
from ..protocols.protocol_replay import ProtocolActionScorer, ProtocolContinuationModel


@dataclass(frozen=True)
class ContainerContinuationReplay:
    model: ProtocolContinuationModel
    scorer: ProtocolActionScorer
    stability: HandlerRepeatStability
    scratch_root: Path
    stubs_factory: StubFactory
    policy: ModelContinuationPolicy
    replay_handler: type[HandlerContinuationReplay]


def create_replay_container(
    *,
    model: ProtocolContinuationModel,
    scratch_root: Path,
    scorer: ProtocolActionScorer | None = None,
    stubs_factory: StubFactory = SandboxTools,
    policy: ModelContinuationPolicy | None = None,
) -> ModelONEXContainer:
    bindings = ContainerContinuationReplay(
        model,
        scorer or HandlerCompactionActionScore(),
        load_component_handler("stability", HandlerRepeatStability)(),
        scratch_root,
        stubs_factory,
        policy or load_continuation_policy(),
        load_component_handler("replay", HandlerContinuationReplay),
    )
    container = ModelONEXContainer()
    run_coro_sync(
        container.service_registry.register_instance(
            interface=ContainerContinuationReplay,
            instance=bindings,
        )
    )
    return container
