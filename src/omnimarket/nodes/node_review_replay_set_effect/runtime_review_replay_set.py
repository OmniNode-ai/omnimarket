# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command-line dispatch for the review replay set effect (OMN-20910)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_review_replay_set_effect.handlers.handler_review_replay_recall import (
    HandlerReviewReplayFreezeBar,
    HandlerReviewReplayRecall,
)
from omnimarket.nodes.node_review_replay_set_effect.handlers.handler_review_replay_set import (
    HandlerReviewReplaySet,
)
from omnimarket.nodes.node_review_replay_set_effect.models import (
    ModelFreezeRecallBarRequest,
    ModelReviewReplayRecallRequest,
    ModelReviewReplaySetRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument(
        "--operation", choices=("freeze", "freeze-bar", "recall"), default="freeze"
    )
    args = parser.parse_args(argv)
    try:
        text = args.request.read_text(encoding="utf-8")
        if args.operation == "freeze-bar":
            bar_result = HandlerReviewReplayFreezeBar().handle(
                ModelFreezeRecallBarRequest.model_validate_json(text)
            )
            sys.stdout.write(bar_result.model_dump_json(indent=2) + "\n")
            return 0
        if args.operation == "recall":
            recall_result = HandlerReviewReplayRecall().handle(
                ModelReviewReplayRecallRequest.model_validate_json(text)
            )
            sys.stdout.write(recall_result.model_dump_json(indent=2) + "\n")
            return 0 if recall_result.passed else 1
        result = HandlerReviewReplaySet().handle(
            ModelReviewReplaySetRequest.model_validate_json(text)
        )
    except (OSError, ValueError, ValidationError) as exc:
        sys.stderr.write(f"review replay set: {exc}\n")
        return 2
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 1 if result.short_by else 0
