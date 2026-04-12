# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test: HandlerPrLifecycleStateReducer must accept correlation_id kwarg.

Reproduces the crash reported in OMN-8533:
  TypeError: handle() got an unexpected keyword argument 'correlation_id'

The orchestrator calls the reducer via the ProtocolStateReducerHandler interface:
  await reducer.handle(
      correlation_id=...,
      classified=...,
      dry_run=...,
      inventory_only=...,
      fix_only=...,
      merge_only=...,
  )

This test asserts that call succeeds and returns a ReducerResult.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    ReducerResult,
    TriageRecord,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)


@pytest.mark.unit
class TestHandlerPrLifecycleStateReducerKwargContract:
    """OMN-8533: handler must satisfy ProtocolStateReducerHandler call signature."""

    def test_handle_accepts_correlation_id_kwarg_no_prs(self) -> None:
        """handle() must not raise TypeError when called with correlation_id kwarg."""
        handler = HandlerPrLifecycleStateReducer()
        correlation_id = uuid4()

        result = asyncio.run(
            handler.handle(
                correlation_id=correlation_id,
                classified=(),
                dry_run=False,
                inventory_only=False,
                fix_only=False,
                merge_only=False,
            )
        )

        assert isinstance(result, ReducerResult)

    def test_handle_returns_merge_intent_for_green_pr(self) -> None:
        """handle() classifies green PRs as MERGE intents."""
        handler = HandlerPrLifecycleStateReducer()
        correlation_id = uuid4()
        pr = TriageRecord(
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            category=EnumPrCategory.GREEN,
        )

        result = asyncio.run(
            handler.handle(
                correlation_id=correlation_id,
                classified=(pr,),
                dry_run=False,
                inventory_only=False,
                fix_only=False,
                merge_only=False,
            )
        )

        assert isinstance(result, ReducerResult)
        assert result.merge_count == 1
        assert result.fix_count == 0

    def test_handle_returns_fix_intent_for_red_pr(self) -> None:
        """handle() classifies red PRs as FIX intents."""
        handler = HandlerPrLifecycleStateReducer()
        correlation_id = uuid4()
        pr = TriageRecord(
            pr_number=99,
            repo="OmniNode-ai/omnibase_core",
            category=EnumPrCategory.RED,
        )

        result = asyncio.run(
            handler.handle(
                correlation_id=correlation_id,
                classified=(pr,),
                dry_run=False,
                inventory_only=False,
                fix_only=False,
                merge_only=False,
            )
        )

        assert isinstance(result, ReducerResult)
        assert result.fix_count == 1
        assert result.merge_count == 0

    def test_dry_run_still_returns_result(self) -> None:
        """dry_run=True must return a ReducerResult without crashing."""
        handler = HandlerPrLifecycleStateReducer()
        pr = TriageRecord(
            pr_number=7,
            repo="OmniNode-ai/omnidash",
            category=EnumPrCategory.GREEN,
        )

        result = asyncio.run(
            handler.handle(
                correlation_id=uuid4(),
                classified=(pr,),
                dry_run=True,
                inventory_only=False,
                fix_only=False,
                merge_only=False,
            )
        )

        assert isinstance(result, ReducerResult)
