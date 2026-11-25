# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in node command for the workflow tests: records each step key it runs and answers as scripted.

``read``: installed per FAKE_INSTALLED, running from reading number FAKE_RUNNING_AFTER on (0 = never).
``install``: answers installed. ``bus-append``: appends the rows on stdin to FAKE_LEDGER (the bus append command's place).
``merge-sha <repo>#<number>``: answers a fixed sha per number. ``batch-step <verb>``: runs the real batch verb
with the child read and install steps set to this stand-in.
Every run appends ``ONEX_RUN_STEP_KEY`` to FAKE_STEP_LOG, so a test counts how often each step ran.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch


def batch_step(verb: str) -> int:
    from omnimarket.nodes.node_prove_running_workflow import (
        runtime_prove_running as runtime,
    )

    cfg = runtime.contract_config()
    cfg["steps"].update(
        read_status=["{python}", __file__, "read"],
        install=["{python}", __file__, "install"],
        read_running=["{python}", __file__, "read"],
    )
    # The broker is out of a unit test: the batch input is the publication the test holds under the declared topic.
    from pathlib import Path

    from omnimarket.handlers.floor_projection import InMemorySnapshotSource
    from omnimarket.models.floor_projection import ModelFloorProjectionReadOverlay

    topic = "onex.snapshot.projection.floor-integration.v1"
    held = {topic: [Path(os.environ["FAKE_INTEGRATION_PUBLICATION"]).read_bytes()]}
    overlay = ModelFloorProjectionReadOverlay.model_validate(
        {
            "bus": "inmemory",
            "read_window_s": 1.0,
            "projections": {"integration": {"topic": topic, "max_age_s": 7800}},
        }
    )
    with (
        patch.object(runtime, "contract_config", return_value=cfg),
        patch.object(runtime, "projection_overlay", return_value=overlay),
        patch.object(runtime, "batch_hosts", return_value=("h051", "h053")),
        patch.object(
            runtime, "snapshot_source", return_value=InMemorySnapshotSource(held)
        ),
    ):
        return runtime.main([verb])


def main() -> int:
    verb = sys.argv[1]
    if verb == "merge-sha":
        repo, number = sys.argv[2].split("#")
        assert repo == "omnimarket"
        print(f"{int(number):040x}")
        return 0
    if verb == "batch-step":
        verb = sys.argv[2]
    key = os.environ["ONEX_RUN_STEP_KEY"]
    with open(os.environ["FAKE_STEP_LOG"], "a", encoding="utf-8") as log:
        log.write(f"{verb} {key}\n")
    if sys.argv[1] == "batch-step":
        return batch_step(verb)
    if verb == "bus-append":
        # The bus append command: rows on stdin (``--rows-file -``), one JSON receipt on stdout.
        with open(os.environ["FAKE_LEDGER"], "a", encoding="utf-8") as ledger:
            rows = sys.stdin.read()
            ledger.write(rows if rows.endswith("\n") else rows + "\n")
        print(json.dumps({"status": "accepted"}))
        return 0
    step = json.loads(sys.stdin.read())
    if verb == "install":
        print(json.dumps({"installer": "fake", "detail": "installed"}))
        return 0
    number = int(key.rsplit("/", 1)[-1]) if "/read_running/" in key else 0
    after = int(os.environ.get("FAKE_RUNNING_AFTER", "0"))
    running = after > 0 and number >= after
    print(
        json.dumps(
            {
                "installed": os.environ.get("FAKE_INSTALLED", "1") == "1",
                "running": running,
                "host": step["change"]["target"],
                "revision": step["change"]["merge_sha"][:12],
                "observed": "dispatch seen in the runtime log"
                if running
                else "no dispatch yet",
                "detail": f"reading {number}",
                "read_at": f"2026-11-16T16:{number:02d}:00Z",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
