# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_skill_dispatch_engine_orchestrator.

Post OMN-14806: the node exposes the canonical def-B dispatch entrypoint
``handle(request: ModelSkillRequest) -> ModelSkillResult``. These tests drive the
handler through the REAL runtime dispatch resolution — the shared auto-wiring
``_make_dispatch_callback``, which binds ``handle``/``handle_async`` and otherwise
binds ``_missing_handle`` — proving the handler is not merely registered/routable
but EXECUTABLE. They also cover the direct def-B behavior: ``dry_run``
short-circuits, the live path routes through the real RSD + self-healing
composition (a bare shim invocation with no backlog access resolves to
``no_candidates`` — an honest empty cycle, not a ``"dispatched"`` placeholder),
and malformed skill requests are rejected at the model boundary.

RED before OMN-14806: the handler exposed only ``dispatch``/``handle_skill_requested``
— NEITHER ``handle`` nor ``handle_async`` — so ``_make_dispatch_callback`` bound
``_missing_handle`` and the first dispatch raised ``ModelOnexError``
("does not expose a callable handle() ..."). GREEN after: the def-B ``handle`` is
bound and returns a typed ``ModelSkillResult``.

Related: OMN-14806 (def-B regen), OMN-14510 (missing-handle burn-down),
OMN-13834 (router rebuild), OMN-8821 (original scaffold).
"""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from pydantic import ValidationError

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_skill_requested import (
    HandlerSkillRequested,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models import (
    ModelSkillRequest,
    ModelSkillResult,
    SkillResultStatus,
)

_SKILL_PATH = "omniclaude/plugins/onex/skills/dispatch_engine/SKILL.md"


class TestGoldenChainDispatchEngine:
    @pytest.mark.unit
    async def test_dispatch_resolution_binds_handle_and_executes(self) -> None:
        """Runtime dispatch resolution binds ``handle`` (not ``_missing_handle``).

        Drives the handler through the SAME ``_make_dispatch_callback`` the shared
        runtime uses at wiring time. RED on the pre-regen tree: the callback bound
        ``_missing_handle`` and this dispatch raised ``ModelOnexError``. GREEN
        after: the def-B ``handle`` executes and yields a real ``ModelSkillResult``
        (normalized into the dispatch result's ``output_events``).
        """
        handler = HandlerSkillRequested(event_bus=EventBusInmemory())
        callback = _make_dispatch_callback(handler)
        envelope = ModelSkillRequest(
            skill_name="dispatch_engine", skill_path=_SKILL_PATH, dry_run=True
        ).model_dump(mode="json")

        result = await callback(envelope)

        assert result is not None
        events = list(result.output_events)
        assert len(events) == 1
        skill_result = events[0]
        assert isinstance(skill_result, ModelSkillResult)
        assert skill_result.skill_name == "dispatch_engine"
        assert skill_result.status is SkillResultStatus.DRY_RUN

    @pytest.mark.unit
    async def test_handle_dry_run_short_circuits(self) -> None:
        handler = HandlerSkillRequested(event_bus=EventBusInmemory())
        result = await handler.handle(
            ModelSkillRequest(
                skill_name="dispatch_engine", skill_path=_SKILL_PATH, dry_run=True
            )
        )
        assert isinstance(result, ModelSkillResult)
        assert result.status is SkillResultStatus.DRY_RUN
        assert result.skill_name == "dispatch_engine"
        assert result.skill_path == _SKILL_PATH

    @pytest.mark.unit
    async def test_handle_routes_live_no_candidates(self) -> None:
        handler = HandlerSkillRequested(event_bus=EventBusInmemory())
        result = await handler.handle(
            ModelSkillRequest(
                skill_name="dispatch_engine",
                skill_path=_SKILL_PATH,
                args={"scope": "post-merge"},
                dry_run=False,
            )
        )
        # No candidate tickets on the bare shim path -> honest empty cycle.
        assert result.status is SkillResultStatus.NO_CANDIDATES
        assert result.run_id.startswith("dispatch-engine-")
        assert result.worker_specs == ()
        assert result.total_selected == 0
        assert result.args == {"scope": "post-merge"}

    @pytest.mark.unit
    def test_skill_path_must_end_with_skill_md(self) -> None:
        with pytest.raises(ValidationError, match=r"SKILL\.md"):
            ModelSkillRequest(
                skill_name="dispatch_engine", skill_path="not-a-skill-file.txt"
            )

    @pytest.mark.unit
    def test_blank_skill_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="skill_name"):
            ModelSkillRequest(skill_name="   ", skill_path="x/SKILL.md")
