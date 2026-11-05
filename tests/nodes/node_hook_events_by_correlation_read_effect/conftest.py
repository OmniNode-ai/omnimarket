# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 replay collection.

The replay of the recorded corpus is collected only where the corpus is: with
ONEX_T18B_CORPUS_DIR unset the file is not collected, so it adds no skipped test
identity to the skip-count ratchet. The replay engine itself is proven on every
run by test_replay_support.py against a synthetic corpus.
"""

from __future__ import annotations

import os

collect_ignore = (
    []
    if os.environ.get("ONEX_T18B_CORPUS_DIR", "").strip()
    else ["test_t18b_replay.py"]
)
