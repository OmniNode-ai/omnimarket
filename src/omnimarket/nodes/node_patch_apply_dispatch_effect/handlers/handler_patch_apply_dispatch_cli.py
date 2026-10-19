# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host ingress: execute the declared patch apply dispatch node through RuntimeLocal's event bus."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.runtime.runtime_local import RuntimeLocal

from ..models import (
    ModelPatchApplyDispatchRequest,
    ModelPatchApplyDispatchResult,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def dispatch(request: ModelPatchApplyDispatchRequest) -> ModelPatchApplyDispatchResult:
    with tempfile.TemporaryDirectory(prefix="patch-apply-dispatch-") as scratch:
        input_path = Path(scratch) / "request.json"
        input_path.write_text(request.model_dump_json())
        runtime = RuntimeLocal(
            workflow_path=CONTRACT,
            input_path=input_path,
            state_root=Path(scratch) / "runtime",
            timeout=600,
        )
        outcome = runtime.run()
    if outcome is not EnumWorkflowResult.COMPLETED:
        raise RuntimeError(f"bus dispatch {outcome.value}: {runtime.last_error}")
    result = runtime.handler_result
    if not isinstance(result, ModelPatchApplyDispatchResult):
        raise RuntimeError("runtime returned no typed patch-apply-dispatch terminal")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", type=datetime.fromisoformat)
    parser.add_argument(
        "--omni-home",
        dest="registry_root",
        type=Path,
        default=os.environ.get("OMNI_HOME"),
    )
    parser.add_argument(
        "--ledger", type=Path, default=os.environ.get("ONEX_LEDGER_PATH")
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--registry",
        type=Path,
        default=os.environ.get("ONEX_REPO_REGISTRY"),
        help="Canonical clone registry YAML (or ONEX_REPO_REGISTRY)",
    )
    parser.add_argument("--max-age-hours", type=float, default=48.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.registry_root is None:
        parser.error("OMNI_HOME (or --omni-home) is required")
    root = Path(os.environ.get("ONEX_STATE_DIR", args.registry_root / ".onex_state"))
    try:
        if args.registry is None:
            parser.error("--registry (or ONEX_REPO_REGISTRY) is required")
        registry = yaml.safe_load(args.registry.read_text())
        repos = tuple(dict.fromkeys(row["name"] for row in registry["repositories"]))
        result = dispatch(
            ModelPatchApplyDispatchRequest(
                now=args.now or datetime.now(UTC),
                registry_root=args.registry_root,
                ledger_path=args.ledger
                or args.registry_root / "docs/tracking/ROLLING_WORK_LEDGER.md",
                state_dir=args.state_dir or root / "patch-apply-dispatch",
                repos=repos,
                max_age_hours=args.max_age_hours,
                apply=not args.dry_run,
            )
        )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        RuntimeError,
        yaml.YAMLError,
    ) as exc:
        sys.stderr.write(f"PATCH-APPLY-DISPATCH UNKNOWN {exc}\n")
        return 1
    sys.stdout.write(result.summary + "\n")
    for finding in result.findings:
        sys.stdout.write(
            f"  {finding.disposition} {finding.patch} {finding.detail}".rstrip() + "\n"
        )
    for error in result.errors:
        sys.stderr.write(f"patch-apply-dispatch: {error}\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
