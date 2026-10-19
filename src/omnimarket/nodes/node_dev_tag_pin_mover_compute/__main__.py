# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for node_dev_tag_pin_mover_compute: the pin-move tick calls this.

Usage:
    python -m omnimarket.nodes.node_dev_tag_pin_mover_compute \
        --pyproject pyproject.toml --distribution omnibase-infra \
        --repository-url https://github.com/OmniNode-ai/omnibase_infra.git \
        --tag dev-20261018005623-b5f4c2e36e72 --tests not_run

With ``--tests not_run`` the candidate is written in place so the caller can
lock and test it, and the exit is 3. With ``--tests passed`` the move is
written and the exit is 0. Every other refusal (consumer_tests_failed,
not_a_dev_tag, no_tag_pin, not_forward) leaves the file alone and exits 1. The
only write on a refusal is the candidate of ``--tests not_run``. The result
prints as JSON.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_mover import (
    HandlerDevTagPinMover,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_mover import (
    EnumConsumerTestOutcome,
    EnumPinMoveRefusal,
    ModelDevTagPinMoveRequest,
)

EXIT_CANDIDATE_WRITTEN = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--tests",
        choices=[outcome.value for outcome in EnumConsumerTestOutcome],
        default=EnumConsumerTestOutcome.NOT_RUN.value,
    )
    args = parser.parse_args(argv)

    result = HandlerDevTagPinMover().handle(
        ModelDevTagPinMoveRequest(
            pyproject_text=args.pyproject.read_text(),
            distribution=args.distribution,
            repository_url=args.repository_url,
            new_tag=args.tag,
            consumer_tests=EnumConsumerTestOutcome(args.tests),
        )
    )
    sys.stdout.write(result.model_dump_json(exclude={"candidate_text"}) + "\n")
    if result.accepted:
        args.pyproject.write_text(result.candidate_text)
        return 0
    if result.refusal is EnumPinMoveRefusal.CONSUMER_TESTS_NOT_RUN:
        args.pyproject.write_text(result.candidate_text)
        return EXIT_CANDIDATE_WRITTEN
    return 1


if __name__ == "__main__":
    sys.exit(main())
