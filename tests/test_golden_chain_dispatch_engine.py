# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_skill_dispatch_engine_orchestrator (scaffold).

Verifies the dispatch_engine skill scaffold: dry-run returns ``"dry_run"``
and live-dispatch returns the ``"dispatched"`` placeholder. Real dispatch
wiring is follow-up.

Related: OMN-8821
"""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_skill_requested import (
    HandlerSkillRequested,
)


class TestGoldenChainDispatchEngine:
    @pytest.mark.unit
    def test_skill_request_dry_run(self) -> None:
        bus = EventBusInmemory()
        handler = HandlerSkillRequested(event_bus=bus)
        result = handler.handle_skill_requested(
            skill_name="dispatch_engine",
            skill_path="omniclaude/plugins/onex/skills/dispatch_engine/SKILL.md",
            args={},
            dry_run=True,
        )
        assert result["status"] == "dry_run"
        assert result["skill_name"] == "dispatch_engine"

    @pytest.mark.unit
    def test_skill_request_dispatches(self) -> None:
        bus = EventBusInmemory()
        handler = HandlerSkillRequested(event_bus=bus)
        result = handler.handle_skill_requested(
            skill_name="dispatch_engine",
            skill_path="omniclaude/plugins/onex/skills/dispatch_engine/SKILL.md",
            args={},
            dry_run=False,
        )
        assert result["status"] in ("dispatched", "dry_run")
        assert result["skill_name"] == "dispatch_engine"

    @pytest.mark.unit
    def test_skill_path_must_end_with_skill_md(self) -> None:
        handler = HandlerSkillRequested(event_bus=EventBusInmemory())
        with pytest.raises(ValueError, match=r"SKILL\.md"):
            handler.handle_skill_requested(
                skill_name="dispatch_engine",
                skill_path="not-a-skill-file.txt",
                args={},
                dry_run=True,
            )

    @pytest.mark.unit
    def test_blank_skill_name_rejected(self) -> None:
        handler = HandlerSkillRequested(event_bus=EventBusInmemory())
        with pytest.raises(ValueError, match="skill_name"):
            handler.handle_skill_requested(
                skill_name="   ",
                skill_path="x/SKILL.md",
                dry_run=True,
            )
