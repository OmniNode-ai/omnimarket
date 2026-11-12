# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command runner protocol of node_stability_stack_compose_effect.

One sync call runs one argv tuple with one environment and one timeout and
returns the typed output; no shell is ever involved. The live adapter runs
``subprocess.run`` with the argv list; the tests script a fake. The handler
raises nothing through here: a runner exception is the handler's to turn into
a failed step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_stability_stack_compose_effect.models.model_stack_compose import (
    ModelCommandOutput,
)


@runtime_checkable
class ProtocolStackCommandRunner(Protocol):
    """Runs one argv tuple verbatim and reports what the process answered."""

    def run(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> ModelCommandOutput:
        """Run ``argv`` with ``env`` and kill it past ``timeout_seconds``."""
        ...
