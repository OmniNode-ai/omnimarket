# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for PrPolishDispatchAdapter (OMN-9284).

Injects a recording spawner so we assert the exact argv passed to
``claude -p`` AND the breadcrumb directory is created on disk. Both
assertions are required — per the diagnosis, the failure mode is "handler
returns success with zero real side effect," so tests must observe the side
effect, not just the return value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_pr_polish_dispatch import (
    PrPolishDispatchAdapter,
)


class _RecordingSpawner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        stdout: int,
        stderr: int,
        start_new_session: bool,
        env: dict[str, str] | None,
    ) -> object:
        self.calls.append(
            {
                "argv": list(argv),
                "stdout_is_fd": isinstance(stdout, int),
                "stderr_is_fd": isinstance(stderr, int),
                "start_new_session": start_new_session,
                "env": env,
            }
        )
        return object()


@pytest.mark.unit
class TestPrPolishDispatchAdapter:
    async def test_dispatch_review_fix_spawns_claude_and_writes_breadcrumb(
        self, tmp_path: Path
    ) -> None:
        spawner = _RecordingSpawner()
        adapter = PrPolishDispatchAdapter(
            claude_bin="claude-test",
            state_dir=tmp_path,
            spawner=spawner,
        )

        result = await adapter.dispatch_review_fix(
            "OmniNode-ai/omnimarket", 42, "OMN-8085"
        )

        assert "dispatched review-fix" in result
        assert len(spawner.calls) == 1
        call = spawner.calls[0]
        argv = list(call["argv"])  # type: ignore[arg-type]
        assert argv[0] == "claude-test"
        assert argv[1] == "-p"
        assert "/onex:pr_polish" in argv[2]
        assert "--repo OmniNode-ai/omnimarket" in argv[2]
        assert "--pr 42" in argv[2]
        assert "--ticket OMN-8085" in argv[2]
        assert call["start_new_session"] is True

        polish_root = tmp_path / "pr-polish"
        run_dirs = list(polish_root.iterdir())
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]
        assert run_dir.name.startswith("OmniNode-ai-omnimarket-42-")
        breadcrumb = json.loads((run_dir / "dispatch.json").read_text())
        assert breadcrumb["kind"] == "review-fix"
        assert breadcrumb["repo"] == "OmniNode-ai/omnimarket"
        assert breadcrumb["pr_number"] == 42
        assert breadcrumb["ticket_id"] == "OMN-8085"
        assert breadcrumb["argv"] == argv

    async def test_dispatch_review_fix_without_ticket_omits_flag(
        self, tmp_path: Path
    ) -> None:
        spawner = _RecordingSpawner()
        adapter = PrPolishDispatchAdapter(
            claude_bin="claude", state_dir=tmp_path, spawner=spawner
        )

        await adapter.dispatch_review_fix("OmniNode-ai/omnimarket", 99, None)

        argv = list(spawner.calls[0]["argv"])  # type: ignore[arg-type]
        assert "--ticket" not in argv[2]

    async def test_dispatch_coderabbit_reply_uses_coderabbit_skill(
        self, tmp_path: Path
    ) -> None:
        spawner = _RecordingSpawner()
        adapter = PrPolishDispatchAdapter(
            claude_bin="claude", state_dir=tmp_path, spawner=spawner
        )

        result = await adapter.dispatch_coderabbit_reply("OmniNode-ai/omnimarket", 7)

        assert "dispatched coderabbit-reply" in result
        argv = list(spawner.calls[0]["argv"])  # type: ignore[arg-type]
        assert "/onex:coderabbit_triage" in argv[2]
        assert "--repo OmniNode-ai/omnimarket" in argv[2]
        assert "--pr 7" in argv[2]

        run_dirs = list((tmp_path / "pr-polish").iterdir())
        assert len(run_dirs) == 1
        breadcrumb = json.loads((run_dirs[0] / "dispatch.json").read_text())
        assert breadcrumb["kind"] == "coderabbit-reply"

    async def test_multiple_dispatches_each_get_unique_run_dir(
        self, tmp_path: Path
    ) -> None:
        spawner = _RecordingSpawner()
        adapter = PrPolishDispatchAdapter(
            claude_bin="claude", state_dir=tmp_path, spawner=spawner
        )

        await adapter.dispatch_review_fix("o/r", 1, None)
        await adapter.dispatch_review_fix("o/r", 1, None)

        run_dirs = list((tmp_path / "pr-polish").iterdir())
        assert len(run_dirs) == 2
        assert run_dirs[0].name != run_dirs[1].name
