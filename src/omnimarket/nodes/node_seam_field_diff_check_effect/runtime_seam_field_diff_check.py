# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge check's entry point: a producer-consumer pair this change adds or reshapes matches in declared message shape (OMN-24121).

env -u PYTHONPATH uv run python -m omnimarket.nodes.node_seam_field_diff_check_effect.runtime_seam_field_diff_check --base-ref origin/dev
Exit 0 clean, 1 refused (each new difference named), 2 the base or the contracts could not be read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.models.seam_field_diff.model_seam_field_diff_check import (
    ModelSeamFieldDiffCheckRequest,
)
from omnimarket.nodes.node_seam_field_diff_check_effect.handlers.handler_seam_field_diff_check import (
    HandlerSeamFieldDiffCheck,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True, help="the base branch's revision")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = HandlerSeamFieldDiffCheck().handle(
        ModelSeamFieldDiffCheckRequest(
            repo_root=str(args.repo_root), base_ref=args.base_ref
        )
    )
    for line in result.output_lines:
        sys.stdout.write(f"{line}\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
