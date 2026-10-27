# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scan a repository and print each refused reference."""

from __future__ import annotations

import argparse
import sys

from .handlers.handler_retired_model_reference_check import (
    HandlerRetiredModelReferenceCheck,
)
from .models.model_retired_model_reference_check import (
    ModelRetiredModelReferenceCheckRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = HandlerRetiredModelReferenceCheck().handle(
        ModelRetiredModelReferenceCheckRequest(repo_root=args.repo_root)
    )
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
