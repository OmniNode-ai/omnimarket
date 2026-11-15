# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_review_gate_context_effect (OMN-24614): the golden chain and the error chains the floor's emission contract names.

Golden chain: a decision is posted on the exact head, read back, and the chain holds one start and one success. Error chains:
a head the read cannot reach, a post the write node refuses and a post the readback cannot see each close the chain with one
start and one typed failure code. The rest of the node's cases (unchanged repeat, new head, injected breaks) are in
tests/unit/nodes/node_review_gate_context_effect.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_review_gate_context_effect.adapters.inmemory_head_statuses import (
    InMemoryHeadStatuses,
)
from omnimarket.nodes.node_review_gate_context_effect.models.model_review_context_post import (
    EnumReviewContextPostOutcome,
)
from tests.unit.nodes.node_review_gate_context_effect.test_review_gate_context_effect import (
    EnumReviewContextState,
    command,
    make_handler,
)

pytestmark = pytest.mark.unit


def _phases(sink: object) -> list[tuple[str, str | None]]:
    return [(e.phase, e.failure_code) for e in sink.events]  # type: ignore[attr-defined]


def test_golden_chain_post_on_the_exact_head() -> None:
    handler, sink, runs = make_handler()
    result = handler.handle(command(EnumReviewContextState.FAILURE))
    assert result.outcome is EnumReviewContextPostOutcome.POSTED
    assert _phases(sink) == [("start", None), ("success", None)]
    assert runs.posted[0]["head_sha"] == result.head_sha


@pytest.mark.parametrize(
    ("runs", "outcome", "code"),
    [
        (
            InMemoryHeadStatuses(fail_reads=True),
            EnumReviewContextPostOutcome.FAILED_READ,
            "review_context_read_failed",
        ),
        (
            InMemoryHeadStatuses(fail_posts=True),
            EnumReviewContextPostOutcome.FAILED_POST,
            "review_context_post_failed",
        ),
        (
            InMemoryHeadStatuses(drop_posts=True),
            EnumReviewContextPostOutcome.FAILED_READBACK,
            "review_context_readback_mismatch",
        ),
    ],
)
def test_error_chain_closes_with_one_typed_failure(
    runs: InMemoryHeadStatuses,
    outcome: EnumReviewContextPostOutcome,
    code: str,
) -> None:
    handler, sink, _ = make_handler(reader=runs, poster=runs)
    result = handler.handle(command(EnumReviewContextState.SUCCESS))
    assert result.outcome is outcome
    assert _phases(sink) == [("start", None), ("failure", code)]
