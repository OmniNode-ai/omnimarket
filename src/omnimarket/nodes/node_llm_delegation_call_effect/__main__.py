# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host dispatcher CLI. Prompt on stdin, one typed receipt on stdout."""

from __future__ import annotations

import argparse
import sys

import yaml

from .handlers.handler_codex_harness_attempt import HandlerCodexHarnessAttempt
from .models.model_codex_harness_attempt import ModelCodexHarnessRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["codex-harness"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    try:
        request = ModelCodexHarnessRequest(
            prompt=sys.stdin.read(),
            model=args.model,
            timeout_seconds=args.timeout,
        )
        receipt = HandlerCodexHarnessAttempt().handle(request)
    except (ValueError, OSError, yaml.YAMLError):
        # Validation and host-path errors must not echo the caller's prompt.
        sys.stderr.write("codex harness request or host policy invalid\n")
        return 2
    sys.stdout.write(receipt.model_dump_json() + "\n")
    return 0 if receipt.exit_cause == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
