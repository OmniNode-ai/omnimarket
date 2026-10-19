# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI adapter: every friction accounting command travels through the contract's bus."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from omnibase_core.runtime.runtime_local import RuntimeLocal

from ..models.model_friction_synthesis_request import ModelFrictionSynthesisRequest
from ..models.model_friction_synthesis_result import ModelFrictionSynthesisResult


def dispatch(request: ModelFrictionSynthesisRequest) -> ModelFrictionSynthesisResult:
    """Use the existing local bus runtime for filesystem-bound accounting."""
    with tempfile.TemporaryDirectory(prefix="onex-friction-synthesis-") as directory:
        root = Path(directory)
        payload = root / "request.json"
        payload.write_text(request.model_dump_json(), encoding="utf-8")
        runtime = RuntimeLocal(
            Path(__file__).parents[1] / "contract.yaml",
            state_root=root / "state",
            input_path=payload,
            backend_overrides={"event_bus": "inmemory"},
        )
        runtime.run()
        result = runtime.handler_result
        if not isinstance(result, ModelFrictionSynthesisResult):
            raise RuntimeError(
                f"synthesis NOT RUN: {runtime.last_error or 'no terminal event'}"
            )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mechanical friction accounting over the node bus"
    )
    parser.add_argument("handoffs", type=Path, nargs="+")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    request = ModelFrictionSynthesisRequest(
        handoffs=args.handoffs, output=args.output, verify=args.verify
    )
    try:
        result = dispatch(request)
    except RuntimeError as error:
        print(f"UNPROVEN: {error}", file=sys.stderr)
        return 1
    print(result.stderr, end="", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
