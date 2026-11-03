# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: the T18b recorded pairs of this route replay with no unmasked difference.

Pending on T18b (OMN-22158): the test runs once the corpus lands and
ONEX_T18B_CORPUS_DIR names it. See tests/nodes/envelope_example_replay.py.
"""

from __future__ import annotations

import pytest

from tests.nodes import envelope_example_replay as replay_support
from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.unit
ROUTE = support.GET_ROUTE


def _cases() -> list[object]:
    corpus = replay_support.corpus_dir()
    if corpus is None:
        return [
            pytest.param(
                None, marks=pytest.mark.skip(reason=replay_support.SKIP_REASON_UNSET)
            )
        ]
    pairs = replay_support.load_pairs(corpus, ROUTE)
    if not pairs:
        return [
            pytest.param(
                None, marks=pytest.mark.skip(reason=replay_support.SKIP_REASON_NONE)
            )
        ]
    return [pytest.param(pair, id=f"pair-{index}") for index, pair in enumerate(pairs)]


@pytest.mark.parametrize("pair", _cases())
def test_recorded_pair_replays_with_no_unmasked_difference(
    pair: replay_support.Pair,
) -> None:
    assert replay_support.replay(ROUTE, pair) == []
