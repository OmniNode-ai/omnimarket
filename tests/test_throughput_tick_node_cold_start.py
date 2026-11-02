# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The throughput tick decision node must not import the scheduled-fire handler's runtime models on cold start."""

from __future__ import annotations

import subprocess
import sys

_PROBE = (
    "import sys;"
    "import omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision;"
    "print('omnibase_infra.runtime.models' in sys.modules)"
)


def test_tick_decision_import_skips_runtime_models() -> None:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "False"


def test_tick_scheduled_fire_handler_still_importable_from_package() -> None:
    from omnimarket.nodes.node_throughput_tick_decision_compute.handlers import (
        HandlerThroughputTickScheduledFire,
    )

    assert (
        HandlerThroughputTickScheduledFire.__name__
        == "HandlerThroughputTickScheduledFire"
    )
