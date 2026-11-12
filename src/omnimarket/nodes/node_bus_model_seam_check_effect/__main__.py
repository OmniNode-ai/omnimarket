# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the bus-model seam check and print findings or the verdict."""

from __future__ import annotations

import argparse
import sys

from .handlers.handler_bus_model_seam_check import (
    HandlerBusModelSeamCheck,
)
from .models.model_bus_model_seam_check_request import (
    ModelBusModelSeamCheckRequest,
)
from .models.model_bus_model_seam_pair import ModelBusModelSeamPair


def _pin_yaml(pair: ModelBusModelSeamPair) -> str:
    """One pins-list item for a refusing pair, in the policy file's own YAML shape."""
    lines = [
        f"- topic: {pair.topic}",
        f"  producer_model: {pair.producer_model}",
        f"  consumer_model: {pair.consumer_model}",
    ]
    if pair.refused:
        lines.append("  refused:")
        lines.extend(f"  - {name}" for name in pair.refused)
    if pair.missing:
        lines.append("  missing:")
        lines.extend(f"  - {name}" for name in pair.missing)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--policy", default=None)
    parser.add_argument("--list-findings", action="store_true")
    parser.add_argument(
        "--strict-subjects",
        action="store_true",
        help="Grade only the strict models of the policy: exit 1 when one of their pairs refuses.",
    )
    args = parser.parse_args(argv)
    request = ModelBusModelSeamCheckRequest(
        repo_root=args.repo_root, policy_path=args.policy
    )
    result = HandlerBusModelSeamCheck().handle(request)
    if args.list_findings:
        seen: set[tuple[str, str, str]] = set()
        for pair in result.pairs:
            key = (pair.topic, pair.producer_model, pair.consumer_model)
            if (pair.refused or pair.missing) and key not in seen:
                seen.add(key)
                sys.stdout.write(_pin_yaml(pair) + "\n")
        return 0
    if args.strict_subjects:
        for line in result.strict_failures:
            sys.stdout.write(line + "\n")
        return int(bool(result.strict_failures))
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
