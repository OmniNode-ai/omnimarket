# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Freeze the review replay set from a request file (OMN-20910).

    python -m omnimarket.nodes.node_review_replay_set_effect --request FILE

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

from omnimarket.nodes.node_review_replay_set_effect.handlers.handler_review_replay_set import (
    HandlerReviewReplaySet,
)
from omnimarket.nodes.node_review_replay_set_effect.models import (
    ModelReviewReplaySetRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = ModelReviewReplaySetRequest.model_validate_json(
            args.request.read_text(encoding="utf-8")
        )
        result = HandlerReviewReplaySet().handle(request)
    except (OSError, ValueError, ValidationError) as exc:
        sys.stderr.write(f"review replay set: {exc}\n")
        return 2
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 1 if result.short_by else 0


if __name__ == "__main__":
    sys.exit(main())
