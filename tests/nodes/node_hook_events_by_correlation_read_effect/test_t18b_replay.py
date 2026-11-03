# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: replay the T18b corpus pairs of this route against the mounted app.

Reads the corpus directory from ONEX_T18B_CORPUS_DIR. AC3 (fixture replay) is
pending on T18b, OMN-22158; this test runs once the corpus lands. It fails on
any difference outside the corpus's named masking rules, and never passes with
zero pairs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.nodes.node_hook_events_by_correlation_read_effect.replay_support import (
    SKIP_REASON,
    replay_all,
    select_pairs,
)

pytestmark = pytest.mark.unit


async def test_corpus_pairs_replay_with_no_unmasked_difference():
    corpus_dir = os.environ.get("ONEX_T18B_CORPUS_DIR", "").strip()
    if not corpus_dir:
        pytest.skip(SKIP_REASON)
    corpus = Path(corpus_dir)
    pairs, _ = select_pairs(corpus)
    if not pairs:
        pytest.skip(SKIP_REASON)
    count, problems = await replay_all(corpus)
    assert count == len(pairs) > 0
    assert not problems, "\n".join(problems)
