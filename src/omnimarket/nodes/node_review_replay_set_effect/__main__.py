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

import sys

from omnimarket.nodes.node_review_replay_set_effect.handlers.cli_review_replay_set import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
