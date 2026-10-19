# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host ingress using the shared contract-driven RuntimeLocal and in-memory bus."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.node_hourly_tick_effect.handlers.handler_hourly_tick import (
    build_record_request,
    prepared_result_path,
)
from omnimarket.nodes.node_hourly_tick_effect.models.model_hourly_tick import (
    ModelHourlyTickRequest,
    ModelHourlyTickResult,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def dispatch(
    request: ModelHourlyTickRequest, state_root: Path
) -> ModelHourlyTickResult:
    """Execute the real handler via contract topic wiring, preserving runtime evidence."""
    state_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hourly-tick-") as scratch:
        input_path = Path(scratch) / "request.json"
        input_path.write_text(request.model_dump_json())
        runtime = RuntimeLocal(
            workflow_path=CONTRACT,
            state_root=state_root,
            input_path=input_path,
            timeout=60,
        )
        outcome = runtime.run()
    if outcome is not EnumWorkflowResult.COMPLETED:
        raise RuntimeError(
            f"hourly-tick: bus dispatch failed: {outcome.value}; {runtime.last_error}"
        )
    result = runtime.handler_result
    if not isinstance(result, ModelHourlyTickResult):
        raise RuntimeError(
            "hourly-tick: runtime completed without the typed handler result"
        )
    return result


def keep_prepared_result(
    root: Path, fire_id: str, result: ModelHourlyTickResult
) -> None:
    """Keep what prepare printed, so record is built from it and not from the agent's copy."""
    path = prepared_result_path(root, fire_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(result.model_dump_json(by_alias=True) + "\n")
    temporary.replace(path)


def build_record_from_files(
    root: Path, fire_id: str, sweep_file: Path
) -> ModelHourlyTickRequest:
    path = prepared_result_path(root, fire_id)
    if not path.is_file():
        raise ValueError(f"hourly-tick: no prepared result for fire {fire_id}")
    prepared = ModelHourlyTickResult.model_validate_json(path.read_text())
    return build_record_request(prepared, sweep_file.read_text(), fire_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request-json",
        help="Typed request JSON; record carries prepared state and sweep",
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--fire-id", default="")
    parser.add_argument(
        "--date", help="Trigger date, retained for the shared host timer interface"
    )
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-sweep", action="store_true")
    parser.add_argument(
        "--record",
        action="store_true",
        help="Build the record request from this fire's prepared result and --sweep-file, "
        "then dispatch record; nothing is retyped",
    )
    parser.add_argument(
        "--sweep-file", type=Path, help="File holding the sweep return, verbatim"
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="With --record: print the request, dispatch none",
    )
    args = parser.parse_args(argv)
    if args.input and args.request_json:
        parser.error("choose --input or --request-json")
    if args.record and (
        args.input or args.request_json or args.dry_run or args.skip_sweep
    ):
        parser.error(
            "--record builds its own request; it takes only --fire-id and --sweep-file"
        )
    if args.record and not (args.sweep_file and args.fire_id):
        parser.error("--record needs --fire-id and --sweep-file")
    if args.build_only and not args.record:
        parser.error("--build-only needs --record")
    root_value = os.environ.get("OMNI_HOME")
    if args.record:
        if not root_value:
            raise ValueError("hourly-tick: set OMNI_HOME")
        request = build_record_from_files(
            Path(root_value), args.fire_id, args.sweep_file
        )
        sys.stderr.write(f"hourly-tick: record request {request.model_dump_json()}\n")
        if args.build_only:
            sys.stdout.write(request.model_dump_json() + "\n")
            return 0
    else:
        raw = args.input.read_text() if args.input else args.request_json
        request = (
            ModelHourlyTickRequest.model_validate_json(raw)
            if raw
            else ModelHourlyTickRequest(
                fire_id=args.fire_id, dry_run=args.dry_run, skip_sweep=args.skip_sweep
            )
        )
    root = request.registry_root or (Path(root_value) if root_value else None)
    if root is None:
        raise ValueError("hourly-tick: set OMNI_HOME")
    evidence = (
        args.state_root
        or root
        / ".onex_state/hourly-tick"
        / str(request.correlation_id)
        / request.operation
    )
    result = dispatch(request, evidence)
    if request.operation == "prepare" and request.fire_id and not request.dry_run:
        keep_prepared_result(root, request.fire_id, result)
    sys.stdout.write(
        json.dumps(result.model_dump(mode="json", by_alias=True), separators=(",", ":"))
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
