# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host ingress for serialized lane dispatch, over RuntimeLocal's event bus.

Write a spec as JSON data, then run ``onex-lane-dispatch --spec-file spec.json
--output-dir out`` to build the canonical hashed record, check the generated
brief and return the runner argv. Add ``--execute`` to call the runner. An
already hashed record is accepted only when its hash and policy still match.
For a JSON list, --execute dispatches each item and returns an ordered ``results``
array of placed, placing, refused or crashed outcomes with reasons. Without it,
every list item is refused before any command. Exit codes are 0 for all placed,
3 for pending placement, 65 for refusal, and 70 for a crash (in that precedence
order from lowest to highest severity).
The deployment supplies ``lane_dispatch/overlay.yaml`` through the standard
overlay roots, including checker/runner argv and the in-memory or Kafka binding.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from pathlib import Path

from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.runtime.runtime_local import RuntimeLocal
from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bind_lane_client_transport,
)

from .models.model_lane_dispatch_effect import (
    ModelLaneDispatchRequest,
    ModelLaneDispatchResult,
)
from .overlay import load_dispatch_overlay

CONTRACT = Path(__file__).with_name("contract.yaml")


def dispatch(request: ModelLaneDispatchRequest) -> ModelLaneDispatchResult:
    """The CLI invokes the declared node, never a handler side path."""
    overlay = load_dispatch_overlay()
    try:
        raw = json.loads(request.record_json)
    except ValueError:
        # Malformed single-record input still reaches the node's typed refusal.
        raw = None
    item_count = len(raw) if isinstance(raw, list) else 1
    backends: dict[str, str] = {"event_bus": overlay.event_bus}
    binding: contextlib.AbstractContextManager[object] = contextlib.nullcontext()
    if overlay.kafka_bootstrap:
        backends["kafka_bootstrap"] = overlay.kafka_bootstrap
        selection, transport = resolve_lane_binding(overlay.bus_lane or "")
        if selection.bootstrap_servers != overlay.kafka_bootstrap:
            raise ValueError("dispatch broker differs from the declared lane")
        if transport is not None:
            binding = bind_lane_client_transport(transport)
    with binding, tempfile.TemporaryDirectory(prefix="lane-dispatch-") as scratch:
        input_path = Path(scratch) / "request.json"
        input_path.write_text(request.model_dump_json(), encoding="utf-8")
        runtime = RuntimeLocal(
            workflow_path=CONTRACT,
            input_path=input_path,
            state_root=Path(scratch) / "runtime",
            # Each item can spend one checker budget and two runner budgets
            # (setup plus placement). A batch must not lose earlier outcomes
            # to the single-record runtime deadline.
            timeout=max(600, item_count * overlay.command_timeout_s * 3 + 60),
            backend_overrides=backends,
        )
        outcome = runtime.run()
    result = runtime.handler_result
    if isinstance(result, ModelLaneDispatchResult):
        return result
    if outcome is not EnumWorkflowResult.COMPLETED:
        raise RuntimeError(f"bus dispatch {outcome.value}: {runtime.last_error}")
    raise RuntimeError("no typed lane dispatch terminal event")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="invoke the runner after the existing checker accepts",
    )
    args = parser.parse_args(argv)
    try:
        result = dispatch(
            ModelLaneDispatchRequest(
                record_json=args.spec_file.read_text(encoding="utf-8"),
                output_dir=str(args.output_dir),
                execute=args.execute,
            )
        )
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"DISPATCH-UNREADABLE: {exc}\n")
        return 69
    sys.stdout.write(result.model_dump_json() + "\n")
    statuses = {item.status for item in result.results} | {result.status}
    if "crashed" in statuses:
        return 70
    if "refused" in statuses:
        return 65
    if "placing" in statuses:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
