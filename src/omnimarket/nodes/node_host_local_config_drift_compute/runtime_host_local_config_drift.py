# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node runtime of the host local config drift read: run it from a host timer or by hand (OMN-24242).

``python -m omnimarket.nodes.node_host_local_config_drift_compute --host <name> --correlation-id <id> < measured.json``: the
measured snapshot (names only) arrives on stdin as JSON, the declaration comes from the node's overlay under
``ONEX_SKILL_OVERLAY_ROOTS``, and the events the run produces are printed one per line as ``<topic> <json>``. Exit 0 when the host
reads WORKING, 1 for drift, 2 for a typed failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from omnimarket.nodes.node_host_local_config_drift_compute.handlers.handler_host_local_config_drift import (
    HandlerHostLocalConfigDrift,
    answer_command,
)
from omnimarket.nodes.node_host_local_config_drift_compute.overlay import (
    declaration_for,
    load_overlay,
)

CONTRACT = Path(__file__).resolve().parent / "contract.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_host_local_config_drift_compute")
    parser.add_argument("--host", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args(argv)
    topics = dict(yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["topics"])
    declaration = declaration_for(load_overlay(os.environ), args.host)
    measured = yaml.safe_load(sys.stdin.read())
    payload = json.dumps(
        {
            "correlation_id": args.correlation_id,
            "declaration": declaration.model_dump(mode="json"),
            "measured": measured,
        }
    ).encode()
    events = answer_command(payload, HandlerHostLocalConfigDrift(), topics)
    for topic, body in events:
        sys.stdout.write(f"{topic} {body.decode()}\n")
    topic_set = {
        topics["host_local_config_drift_failed"]: 2,
        topics["host_local_config_drift_found"]: 1,
    }
    return max((topic_set.get(topic, 0) for topic, _ in events), default=0)
