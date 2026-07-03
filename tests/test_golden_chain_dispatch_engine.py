# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_skill_dispatch_engine_orchestrator.

Exercises the skill-lifecycle shim (``handle_skill_requested``) after the
OMN-13834 router rebuild: ``dry_run`` short-circuits to a ``dry_run`` status, the
live path routes through the real RSD + self-healing composition (a bare shim
invocation with no backlog access resolves to ``no_candidates`` — an honest empty
cycle, not a ``"dispatched"`` placeholder), and the request boundary still rejects
malformed skill requests.

Related: OMN-13834 (router rebuild), OMN-8821 (original scaffold)
"""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_skill_requested import (
    HandlerSkillRequested,
)


class TestGoldenChainDispatchEngine:
    @pytest.mark.unit
    async def test_skill_request_dry_run(self) -> None:
        bus = EventBusInmemory()
        handler = HandlerSkillRequested(event_bus=bus)
        result = await handler.handle_skill_requested(
            skill_name="dispatch_engine",
            skill_path="omniclaude/plugins/onex/skills/dispatch_engine/SKILL.md",
            args={},
            dry_run=True,
        )
        assert result["status"] == "dry_run"
        assert result["skill_name"] == "dispatch_engine"

    @pytest.mark.unit
    async def test_skill_request_routes_live(self) -> None:
        bus = EventBusInmemory()
        handler = HandlerSkillRequested(event_bus=bus)
        result = await handler.handle_skill_requested(
            skill_name="dispatch_engine",
            skill_path="omniclaude/plugins/onex/skills/dispatch_engine/SKILL.md",
            args={},
            dry_run=False,
        )
        # No candidate tickets on the bare shim path → honest empty cycle.
        assert result["status"] == "no_candidates"
        assert result["skill_name"] == "dispatch_engine"
        assert result["run_id"].startswith("dispatch-engine-")
        assert result["worker_specs"] == []

    @pytest.mark.unit
    async def test_skill_path_must_end_with_skill_md(self) -> None:
        handler = HandlerSkillRequested(event_bus=EventBusInmemory())
        with pytest.raises(ValueError, match=r"SKILL\.md"):
            await handler.handle_skill_requested(
                skill_name="dispatch_engine",
                skill_path="not-a-skill-file.txt",
                args={},
                dry_run=True,
            )

    @pytest.mark.unit
    async def test_blank_skill_name_rejected(self) -> None:
        handler = HandlerSkillRequested(event_bus=EventBusInmemory())
        with pytest.raises(ValueError, match="skill_name"):
            await handler.handle_skill_requested(
                skill_name="   ",
                skill_path="x/SKILL.md",
                dry_run=True,
            )
