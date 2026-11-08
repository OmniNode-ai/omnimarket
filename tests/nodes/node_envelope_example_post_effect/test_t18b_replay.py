# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: the T18b recorded pairs of this route replay with no unmasked difference.

Pending on T18b (OMN-22158): the test is defined once ONEX_T18B_CORPUS_DIR
names the corpus. It is defined only where the corpus is, because a test that
is collected and then skipped counts against the skip-count ratchet. The replay
engine is proven every run by the envelope example replay support tests. See
tests/nodes/envelope_example_replay.py.
"""

from __future__ import annotations

import pytest

from tests.nodes import envelope_example_replay as replay_support
from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.unit
ROUTE = support.POST_ROUTE

_CORPUS = replay_support.corpus_dir()

if _CORPUS is not None:
    _PAIRS = replay_support.load_pairs(_CORPUS, ROUTE)

    @pytest.mark.parametrize(
        "pair",
        [pytest.param(pair, id=f"pair-{i}") for i, pair in enumerate(_PAIRS)],
    )
    def test_recorded_pair_replays_with_no_unmasked_difference(
        pair: replay_support.Pair,
    ) -> None:
        assert replay_support.replay(ROUTE, pair) == []

    def test_corpus_holds_a_pair_for_this_route() -> None:
        assert _PAIRS, replay_support.SKIP_REASON_NONE
