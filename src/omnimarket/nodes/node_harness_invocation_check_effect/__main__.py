# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scan a repository (or the named files) and print the harness invocation verdict."""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_harness_invocation_check_effect.handlers.handler_harness_invocation_check import (
    HandlerHarnessInvocationCheck,
)
from omnimarket.nodes.node_harness_invocation_check_effect.models.model_harness_invocation_check import (
    ModelHarnessInvocationCheckRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    result = HandlerHarnessInvocationCheck().handle(
        ModelHarnessInvocationCheckRequest(
            repo_root=args.repo_root, paths=tuple(args.paths)
        )
    )
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
