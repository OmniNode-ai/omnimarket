# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scan a repository's fixture tests and print each refused payload dict."""

from __future__ import annotations

import argparse
import sys

from .handlers.handler_handwritten_event_payload_check import (
    HandlerHandwrittenEventPayloadCheck,
)
from .models.model_handwritten_event_payload_check import (
    ModelHandwrittenEventPayloadCheckRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = HandlerHandwrittenEventPayloadCheck().handle(
        ModelHandwrittenEventPayloadCheckRequest(repo_root=args.repo_root)
    )
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
