# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Preview the installed host's retention policy and record one run."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
)
from omnimarket.nodes.node_host_retention_effect.runtime_host_retention import (
    EXIT_INDETERMINATE,
    EXIT_REFUSED,
)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_tick_arguments(parser)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": "probe_ok"}) + "\n")
        return 0
    host = os.environ.get("ONEX_HOST_NAME", "").strip()
    if not host:
        parser.error("ONEX_HOST_NAME must name the installed host's overlay row")

    result = HandlerHostRetention().handle(
        ModelHostRetentionRequest(host_name=host, dry_run=True)
    )
    code = 0
    if result.status == "refused":
        code = EXIT_REFUSED
    elif result.errors:
        code = EXIT_INDETERMINATE
    line = result.model_dump(
        mode="json",
        include={"host", "dry_run", "status", "totals", "errors", "refusals"},
    )
    write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
