# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Freeze the review replay set from a request file (OMN-20910).

    python -m omnimarket.nodes.node_review_replay_set_effect --request FILE

The recall bar is frozen and scored with ``--operation``:

    ... --operation freeze-bar --request FILE   (ModelFreezeRecallBarRequest)
    ... --operation recall --request FILE       (ModelReviewReplayRecallRequest)

``recall`` exits 1 when the roster's recall is below the frozen bar.

``FILE`` is a ``ModelReviewReplaySetRequest`` as JSON. Prints the result as
JSON: the manifest hash and path, the item count, the shortfall and the items
per stratum. Exit 0 when a set was frozen or found intact, 1 when the request
asked for more than the history holds (``short_by`` above zero), 2 when the
request or a frozen set could not be read.
"""

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


if __name__ == "__main__":
    sys.exit(main())
