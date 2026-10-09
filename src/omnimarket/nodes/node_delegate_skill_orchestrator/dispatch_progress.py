# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep cancellation evidence scoped to one delegation, including child tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_dispatch_progress import (
    DispatchStage,
    ModelDelegationDispatchProgress,
)

current_dispatch_progress: ContextVar[ModelDelegationDispatchProgress | None] = (
    ContextVar("delegation_dispatch_progress", default=None)
)


@contextmanager
def dispatch_stage(stage: DispatchStage) -> Iterator[None]:
    """Preserve the innermost cancelled or timed-out stage through cleanup."""
    progress = current_dispatch_progress.get()
    if progress is None:
        yield
        return
    previous_stage = progress.stage
    progress.stage = stage
    try:
        yield
    except asyncio.CancelledError:
        if progress.cancelled_stage is None:
            progress.cancelled_stage = progress.stage
        raise
    except TimeoutError:
        # A stage's own deadline can expire without cancelling this task.
        # Carry that stage through outer scopes and any subsequent cleanup.
        previous_stage = progress.stage
        raise
    finally:
        progress.stage = previous_stage
