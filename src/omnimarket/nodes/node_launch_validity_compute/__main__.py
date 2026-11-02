# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_launch_validity_compute < recorded.jsonl``: print the would-refuse count line.

Each line of the input is one ``ModelRecordedLaunch`` as JSON. The mode and the limits are
the contract's with the ``launch_validity`` overlay laid over them.
"""

from __future__ import annotations

import sys

from .handlers.handler_launch_validity_replay import HandlerLaunchValidityReplay
from .models.model_launch_validity import (
    ModelLaunchValidityReplayRequest,
    ModelRecordedLaunch,
)
from .overlay import load_launch_validity_policy, replay_window_days


def main(argv: list[str]) -> int:
    if argv:
        sys.stderr.write(
            "usage: python -m omnimarket.nodes.node_launch_validity_compute < recorded.jsonl\n"
        )
        return 2
    launches = [
        ModelRecordedLaunch.model_validate_json(line)
        for line in sys.stdin.read().splitlines()
        if line.strip()
    ]
    policy = load_launch_validity_policy()
    result = HandlerLaunchValidityReplay().handle(
        ModelLaunchValidityReplayRequest(launches=tuple(launches), policy=policy)
    )
    sys.stdout.write(
        HandlerLaunchValidityReplay.count_line(result, replay_window_days(), policy)
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
