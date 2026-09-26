#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one delegated test loop in-process (OMN-19362, first slice).

Binds the orchestrator's ports to the real children and prints ONE compact
result as JSON on stdout. Nothing else goes to stdout.

* prompt   -> node_delegated_test_prompt_compute (pure, in-process)
* delegate -> the sanctioned ``onex`` wrapper, ``onex delegate ... --bus
              inmemory --locus in-process``: the delegate orchestrator runs
              here and only the model call leaves the machine. Each call writes
              its own receipt under ``<state root>/runs/<run id>/``.
* run      -> node_push_validation_effect ``run_focused_test_run``: the test
              executes in a throwaway single-mount container on the lab host
              named by ``ONEX_DTL_HOST`` (the effect's own required variables).
* digest   -> node_pytest_failure_digest_compute (pure, in-process)
* gates    -> the same focused run also runs ruff check, ruff format --check
              and mypy --strict over the written test inside the task
              worktree (OMN-19527); node_code_gate_digest_compute (pure,
              in-process) digests what they printed
* grade    -> node_delegated_test_control_compute (pure, in-process)

The loop receipt is ``<state root>/runs/<correlation id>/loop_receipt.json``,
with each focused-run receipt beside it under ``focused/``.

The ports live in ``omnimarket.delegated_test_loop.loop_ports`` (OMN-19458),
shared with ``onex test-loop run``, which runs the same loop with each focused
run sent over the bus to the lab host instead of over ssh.

Usage:
    uv run python scripts/dtl/run_delegated_test_loop.py --request <request.json> \
        --state-root <state root> --source-clone <local clone of the repository>

``OMNI_HOME`` must be set: the ``onex`` wrapper is resolved from it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from omnimarket.delegated_test_loop.loop_ports import (
    IN_PROCESS_DELEGATE_FLAGS,
    DelegatedTestLoopPorts,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator import (
    HandlerDelegatedTestLoopOrchestrator,
    ModelDelegatedTestLoopRequest,
)
from omnimarket.nodes.node_push_validation_effect import HandlerFocusedTestRunEffect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--source-clone", required=True, type=Path)
    args = parser.parse_args()

    request = ModelDelegatedTestLoopRequest.model_validate_json(
        args.request.read_text()
    )
    ports = DelegatedTestLoopPorts(
        onex=Path(os.environ["OMNI_HOME"]) / "omnibase_infra" / "scripts" / "onex",
        state_root=args.state_root.resolve(),
        source_clone=args.source_clone.resolve(),
        test_path=request.test_path,
        run_focused=HandlerFocusedTestRunEffect().run_sync,
        delegate_flags=IN_PROCESS_DELEGATE_FLAGS,
    )
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(request)
    sys.stdout.write(
        json.dumps(result.model_dump(mode="json"), separators=(",", ":")) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
