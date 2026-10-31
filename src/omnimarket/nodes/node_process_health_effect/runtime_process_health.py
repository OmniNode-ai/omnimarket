# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write the process-health series JSON.

``python -m omnimarket.nodes.node_process_health_effect.runtime_process_health [--output PATH]
[--generated-at STAMP]`` reads the sources the contract names under ``runtime_io``, runs the pure
compute and writes the result to ``runtime_io.output`` (or ``--output``). A source it cannot read is
reported unread in the output; the exit status is 0 whenever the file was written.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.nodes.node_process_health_effect.handlers.handler_process_health_run import (
    HandlerProcessHealthRun,
    OutputPathError,
)
from omnimarket.nodes.node_process_health_effect.models.model_process_health_run import (
    ModelProcessHealthRunRequest,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write the process-health series JSON")
    parser.add_argument("--output", default="")
    parser.add_argument("--generated-at", default="")
    args = parser.parse_args(argv)
    generated_at = (
        datetime.fromisoformat(args.generated_at.replace("Z", "+00:00")).astimezone(UTC)
        if args.generated_at
        else datetime.now(UTC).replace(microsecond=0)
    )
    try:
        result = HandlerProcessHealthRun().handle(
            ModelProcessHealthRunRequest(
                environ=dict(os.environ),
                generated_at=generated_at,
                output_override=args.output,
            )
        )
    except OutputPathError as exc:
        sys.stderr.write(f"process-health: no output path: {exc}\n")
        return 2
    sys.stdout.write(f"process-health: wrote {result.output_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
