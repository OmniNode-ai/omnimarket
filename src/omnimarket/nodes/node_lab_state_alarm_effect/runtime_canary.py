# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the hosted canary (OMN-19998, 17a; plan W23).

``--canary-status FILE`` reads the status document the 17a cycle published (``ModelCanaryStatus``) and prints the
verdict of ``HandlerFloorCanary`` as JSON. Exit 0: the floor, the alarm path and the publisher are all alive. Exit 3:
something is silent, and the job that runs this must fail. Exit 2: the status cannot be read, which is silence as well.
The bounds are the status document's own (they come from the lab's overlay), so nothing is hard-coded in the job.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.models.floor_17a import ModelCanaryInput, ModelCanaryStatus
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_floor_canary import (
    HandlerFloorCanary,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="floor_canary")
    ap.add_argument("--canary-status", required=True, metavar="FILE")
    ap.add_argument("--now", help="an ISO time to judge at; the clock when absent")
    args = ap.parse_args(argv)
    try:
        status = ModelCanaryStatus.model_validate_json(
            Path(args.canary_status).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        sys.stderr.write(
            f"SILENT: the canary status cannot be read ({type(exc).__name__})\n"
        )
        return 2
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.now(UTC)
    )
    verdict = HandlerFloorCanary().handle(
        ModelCanaryInput(
            now=now,
            record_generated_at=status.floor_generated_at,
            heartbeat_at=status.alarm_heartbeat_at,
            floor_max_age_s=status.floor_max_age_s,
            heartbeat_max_age_s=status.heartbeat_max_age_s,
            published_at=status.published_at,
            check_publisher=True,
        )
    )
    sys.stdout.write(verdict.model_dump_json(indent=1) + "\n")
    if verdict.notify:
        sys.stderr.write(f"SILENT ({verdict.state}): {verdict.message}\n")
        return 3
    return 0
