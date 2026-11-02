# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_remote_lane_stop_effect: stop a running lane by name.

Repository markdown outside the knowledge-base allowlist is refused by the doc gate, so the
command is documented here, in the node's own entry point.

Run it through the node runner, or directly:

    onex node node_remote_lane_stop_effect --input request.json --output receipt
    python -m omnimarket.nodes.node_remote_lane_stop_effect < request.json

The request is one ModelRemoteLaneStopRequest as JSON, built by the caller from its overlay:
``lane``, ``reason``, ``stopping_lane``, ``receipt_dir`` (holding ``<lane>/<run id>.json``),
``ledger_path``, ``ledger_command`` (the ledger writer's argv prefix), ``actor``, ``model``,
``hosts`` (per host, by the name the receipt gives it: ``ssh_argv``, ``work_root``,
``reap_script`` with a ``{unit}`` placeholder, or ``local``), ``placement_dir`` (the placement
marker directory) and, for a cloud lane, ``cloud_command`` (taking
``archive|status --session <id> --account <name>``).

The result is the ModelRemoteLaneStopResult as JSON on stdout. Exit 0 when the lane was
stopped, 3 when the lane had already ended, 4 when the lane is unknown, 5 when the stop was
refused. A lane's host, pid, run and cloud session come from its receipt, never the request.
"""

from __future__ import annotations

import sys

from omnimarket.nodes.node_remote_lane_stop_effect.handlers.handler_remote_lane_stop import (
    create_handler,
)
from omnimarket.nodes.node_remote_lane_stop_effect.models.model_remote_lane_stop import (
    ModelRemoteLaneStopRequest,
)

_EXIT = {"stopped": 0, "already-ended": 3, "unknown-lane": 4, "refused": 5}


def main() -> int:
    request = ModelRemoteLaneStopRequest.model_validate_json(sys.stdin.read())
    result = create_handler(request).handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return _EXIT[result.outcome]


if __name__ == "__main__":
    raise SystemExit(main())
