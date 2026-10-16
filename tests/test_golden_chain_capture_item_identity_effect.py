# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the capture item identity effect."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_capture_item_identity_effect.handlers import (
    HandlerCaptureItemIdentity,
)
from omnimarket.nodes.node_capture_item_identity_effect.models import (
    EnumCaptureItemOperation,
    ModelCaptureItemIdentityRequest,
    ModelCaptureItemIdentityResult,
)

pytestmark = pytest.mark.unit

SESSION = "session-golden-1"


def test_golden_chain_two_hooks_of_one_call_share_one_identity(tmp_path: Path) -> None:
    """The lineage hook mints an event id, the content hook adopts it, both number
    their records, and the content record resolves to the lineage reference."""
    handler = HandlerCaptureItemIdentity()
    lineage_minted, content_minted = uuid.uuid4(), uuid.uuid4()

    def ask(**fields: object) -> ModelCaptureItemIdentityResult:
        return handler.handle(
            ModelCaptureItemIdentityRequest(
                item_dir=str(tmp_path), session_id=SESSION, **fields
            )
        )

    lineage_id = ask(
        operation=EnumCaptureItemOperation.CLAIM_EVENT_ID,
        stream="lineage",
        hook_event_name="PostToolUse",
        discriminator="toolu-golden",
        minted_event_id=lineage_minted,
    ).event_id
    content_id = ask(
        operation=EnumCaptureItemOperation.CLAIM_EVENT_ID,
        stream="content",
        hook_event_name="PostToolUse",
        discriminator="toolu-golden",
        minted_event_id=content_minted,
    ).event_id
    assert lineage_id == content_id == lineage_minted

    reference = handler.handle(
        ModelCaptureItemIdentityRequest(
            operation=EnumCaptureItemOperation.CONTENT_RECORD_ID,
            event_id=lineage_id,
            field="tool_response",
        )
    ).content_record_id
    assert reference is not None
    again = handler.handle(
        ModelCaptureItemIdentityRequest(
            operation=EnumCaptureItemOperation.CONTENT_RECORD_ID,
            event_id=content_id,
            field="tool_response",
        )
    ).content_record_id
    assert again == reference

    seqs = [
        ask(
            operation=EnumCaptureItemOperation.NEXT_PRODUCER_SEQ, stream="content"
        ).sequence
        for _ in range(3)
    ]
    assert seqs == [1, 2, 3]
    top = ask(operation=EnumCaptureItemOperation.HIGH_WATER, stream="content")
    assert top.sequence == 3
    assert (
        ask(operation=EnumCaptureItemOperation.HIGH_WATER, stream="lineage").sequence
        == 0
    )


def test_error_chain_unknown_operation_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelCaptureItemIdentityRequest.model_validate({"operation": "mint"})
    assert {err["loc"][0] for err in caught.value.errors()} == {"operation"}


def test_error_chain_unknown_stream_is_refused_by_the_handler(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        HandlerCaptureItemIdentity().handle(
            ModelCaptureItemIdentityRequest(
                operation=EnumCaptureItemOperation.NEXT_PRODUCER_SEQ,
                item_dir=str(tmp_path),
                session_id=SESSION,
                stream="side",
            )
        )


def test_error_chain_a_corrupt_state_file_restarts_the_count(tmp_path: Path) -> None:
    (tmp_path / f"{SESSION}.seq").write_text("{not json", encoding="utf-8")
    result = HandlerCaptureItemIdentity().handle(
        ModelCaptureItemIdentityRequest(
            operation=EnumCaptureItemOperation.NEXT_PRODUCER_SEQ,
            item_dir=str(tmp_path),
            session_id=SESSION,
            stream="lineage",
        )
    )
    assert result.sequence == 1
