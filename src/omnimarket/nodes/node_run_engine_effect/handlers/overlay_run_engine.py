# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the node overlay and build the backend it selects.

No overlay means the in-memory backend: the default carries no deployment fact. An overlay that selects the real
backend must carry that backend's settings; a broken overlay refuses and is never read as an absent one.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_run_engine_effect.backends.backend_in_memory import (
    BackendInMemory,
)
from omnimarket.nodes.node_run_engine_effect.backends.backend_temporal_self_hosted import (
    BackendTemporalSelfHosted,
)
from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    ProtocolRunEngineBackend,
)
from omnimarket.nodes.node_run_engine_effect.backends.step_runner import (
    ProtocolStepRunner,
    SubprocessStepRunner,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumRunEngineBackend,
    ModelRunEngineOverlay,
)

NODE_OVERLAY_ID = "node_run_engine_effect"


def load_run_engine_overlay(
    environ: Mapping[str, str] | None = None,
    explicit: object | None = None,
) -> ModelRunEngineOverlay:
    """The overlay, or the in-memory default when no source supplies one."""
    if explicit is None and find_node_overlay(NODE_OVERLAY_ID, environ) is None:
        return ModelRunEngineOverlay()
    return load_structured_overlay(
        NODE_OVERLAY_ID, ModelRunEngineOverlay, environ, explicit=explicit
    )


def build_backend(
    overlay: ModelRunEngineOverlay, runner: ProtocolStepRunner | None = None
) -> ProtocolRunEngineBackend:
    """The backend the overlay's ``run_engine_backend`` selects."""
    if overlay.run_engine_backend is EnumRunEngineBackend.IN_MEMORY:
        return BackendInMemory(runner or SubprocessStepRunner())
    if overlay.temporal_self_hosted is None:
        raise NodeOverlayError("temporal_self_hosted needs its settings")
    return BackendTemporalSelfHosted(overlay.temporal_self_hosted)


_SHARED: dict[str, ProtocolRunEngineBackend] = {}


def shared_backend(overlay: ModelRunEngineOverlay) -> ProtocolRunEngineBackend:
    """The one backend of this process for this overlay, so each operation routed to the node meets the same runs."""
    key = overlay.model_dump_json()
    if key not in _SHARED:
        _SHARED[key] = build_backend(overlay)
    return _SHARED[key]
