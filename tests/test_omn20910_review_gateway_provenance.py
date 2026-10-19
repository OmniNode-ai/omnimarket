# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20910: the review gateway names the review a delegation call is made for.

CI sends the review's identity in the chat request's ``metadata``. The gateway
copies it onto the delegation call so the effect can record the cost of each
provider attempt under it. A request that does not name its whole identity is
still served; its attempts are simply not recorded as a review's.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.enums.enum_review_call_attempt_kind import EnumReviewCallAttemptKind
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    translate_chat_request,
)

pytestmark = pytest.mark.unit

_METADATA: dict[str, Any] = {
    "repository": "OmniNode-ai/omnimarket",
    "review_run_id": "run-9",
    "pr": "4187",
    "head_sha": "e448de5",
    "voter_id": "sonnet",
    "persona_id": "correctness",
}


def _translate(metadata: dict[str, Any] | None) -> Any:
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": "review this"}]}
    if metadata is not None:
        payload["metadata"] = metadata
    return translate_chat_request(
        payload,
        model="claude-sonnet-5-5",
        endpoint_url="https://provider.invalid/v1/chat/completions",
        secret_ref="cred_ref",
        timeout_seconds=30.0,
    )


def test_a_review_that_names_its_identity_is_carried_onto_the_call() -> None:
    review = _translate(_METADATA).review
    assert review is not None
    assert review.review_run_id == "run-9"
    assert review.repository == "OmniNode-ai/omnimarket"
    assert review.pr_number == 4187
    assert review.head_sha == "e448de5"
    assert (review.voter_id, review.persona_id) == ("sonnet", "correctness")
    assert review.attempt_kind is EnumReviewCallAttemptKind.FIRST
    assert (review.chunk_index, review.chunk_count) == (0, 1)


def test_chunk_and_attempt_fields_are_carried_when_sent() -> None:
    review = _translate(
        {
            **_METADATA,
            "chunk_index": 2,
            "chunk_count": 5,
            "attempt_kind": "json_only_retry",
            "prompt_version": "2.0.0",
            "catalog_version": "c1",
        }
    ).review
    assert review is not None
    assert (review.chunk_index, review.chunk_count) == (2, 5)
    assert review.attempt_kind is EnumReviewCallAttemptKind.JSON_ONLY_RETRY
    assert (review.prompt_version, review.catalog_version) == ("2.0.0", "c1")


@pytest.mark.parametrize(
    "missing",
    ["repository", "review_run_id", "pr", "head_sha", "voter_id", "persona_id"],
)
def test_a_review_missing_part_of_its_identity_is_not_recorded_as_one(
    missing: str,
) -> None:
    metadata = {key: value for key, value in _METADATA.items() if key != missing}
    request = _translate(metadata)
    assert request.review is None
    # OMN-20910: the native Messages protocol carries the user turn in blocks.
    assert [block.text for block in request.messages[0].blocks] == ["review this"]


@pytest.mark.parametrize("pr", ["not-a-number", "0", "-3", True, None])
def test_a_pr_that_is_not_a_positive_number_is_not_recorded_as_a_review(
    pr: Any,
) -> None:
    assert _translate({**_METADATA, "pr": pr}).review is None


def test_a_request_with_no_metadata_carries_no_review() -> None:
    assert _translate(None).review is None
