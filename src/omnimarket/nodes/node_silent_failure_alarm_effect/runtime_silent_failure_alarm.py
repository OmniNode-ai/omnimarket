# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host timer and remote watch invoke the same node over its declared command bus."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from omnibase_core.runtime.runtime_local import RuntimeLocal

from .models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
    ModelSilentFailureAlarmResult,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "operation",
        choices=["tick", "drainer-observe", "drainer-repair"],
        nargs="?",
        default="tick",
    )
    parser.add_argument("--omni-home", dest="registry_root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--config", type=Path, help="server-owned alarm configuration JSON"
    )
    parser.add_argument("--now", type=datetime.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    request = ModelSilentFailureAlarmRequest.model_validate(
        {
            "operation": args.operation,
            "now": args.now or datetime.now(UTC),
            "apply": not args.dry_run,
        }
    )
    previous = os.environ.get("ONEX_SILENT_FAILURE_ALARM_CONFIG")
    if args.config is not None:
        os.environ["ONEX_SILENT_FAILURE_ALARM_CONFIG"] = str(args.config.resolve())
    try:
        options = ModelSilentFailureAlarmOptions.from_environment()
        if (
            args.registry_root is not None
            and args.registry_root.resolve() != options.registry_root.resolve()
        ):
            parser.error("--omni-home differs from the server configuration")
        # RuntimeLocal publishes the request and its typed terminal even for a local timer.
        with TemporaryDirectory(prefix="silent-failure-alarm-") as scratch:
            input_path = Path(scratch) / "request.json"
            input_path.write_text(request.model_dump_json())
            runtime = RuntimeLocal(
                Path(__file__).with_name("contract.yaml"),
                input_path=input_path,
                state_root=Path(scratch) / "runtime",
                run_id=request.request_id,
                backend_overrides={"event_bus": "inmemory"},
                timeout=900,
                host_handlers=True,
            )
            runtime.run()
            result = runtime.handler_result
        if not isinstance(result, ModelSilentFailureAlarmResult):
            sys.stderr.write(
                f"silent_failure_alarm: bus dispatch failed: {runtime.last_error}\n"
            )
            return 2
        # The installer-supplied state directory holds the timer's own terminal evidence.
        if request.operation == "tick" and request.apply:
            state = args.state_dir or options.capture_repair_state_file.parent
            state.mkdir(parents=True, exist_ok=True)
            evidence = state / "last-run.json"
            temporary = state / f".last-run.{request.request_id}.json"
            try:
                temporary.write_text(result.model_dump_json() + "\n")
                temporary.replace(evidence)
            finally:
                temporary.unlink(missing_ok=True)
        sys.stdout.write(result.model_dump_json() + "\n")
        for warning in result.warnings:
            sys.stderr.write(warning + "\n")
        return result.exit_code
    finally:
        if args.config is not None:
            if previous is None:
                os.environ.pop("ONEX_SILENT_FAILURE_ALARM_CONFIG", None)
            else:
                os.environ["ONEX_SILENT_FAILURE_ALARM_CONFIG"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
