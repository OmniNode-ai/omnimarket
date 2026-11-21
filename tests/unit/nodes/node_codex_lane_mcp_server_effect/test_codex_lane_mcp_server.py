# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25178: the Codex lane MCP server lists the four tools and runs each as one pinned command."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_codex_lane_mcp_server_effect.__main__ import build_server
from omnimarket.nodes.node_codex_lane_mcp_server_effect.handlers import (
    CODEX_LANE_MCP_TOOLS,
    HandlerCodexLaneMcpServerEffect,
)
from omnimarket.nodes.node_codex_lane_mcp_server_effect.models import (
    ModelCodexLaneMcpToolCall,
)

ENV = {
    "ONEX_LANE": "lane-x",
    "ONEX_LANE_TICKET": "OMN-25178",
    "OMNI_PLUGIN_ROOT": "/p/plugins/omni",
    "OMNI_HOME": "/nowhere",
}


class Recorder:
    def __init__(self, code: int = 0, out: str = "ok") -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self.code, self.out = code, out

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if "--body-file" in argv:
            self.bodies.append(Path(argv[argv.index("--body-file") + 1]).read_text())
        assert kwargs["env"]["ONEX_LANE"] == "lane-x"
        return subprocess.CompletedProcess(argv, self.code, self.out, "")


def _call(tool: str, run: Recorder, **arguments: str) -> Any:
    return HandlerCodexLaneMcpServerEffect(ENV, run).handle(
        ModelCodexLaneMcpToolCall(tool=tool, arguments=arguments)
    )


def test_server_lists_exactly_the_four_tools() -> None:
    names = [tool.name for tool in asyncio.run(build_server().list_tools())]
    assert (
        sorted(names)
        == sorted(CODEX_LANE_MCP_TOOLS)
        == sorted(
            ["scm_pr_view", "scm_pr_comment", "ledger_status", "pr_handoff_check"]
        )
    )


def test_pr_view_runs_onex_scm_pr_view() -> None:
    run = Recorder()
    result = _call("scm_pr_view", run, target="OmniNode-ai/repo#12")
    assert result.ok
    assert result.output == "ok"
    assert run.calls[0][1:] == [
        "scm",
        "pr-view",
        "OmniNode-ai/repo#12",
        "--bus-lane",
        "dev",
    ]


def test_pr_comment_passes_the_body_by_file_and_pins_the_lane() -> None:
    run = Recorder()
    assert _call("scm_pr_comment", run, target="repo#3", body="hello\nthere").ok
    argv = run.calls[0]
    assert argv[1:5] == ["scm", "pr-comment", "repo#3", "--lane"]
    assert argv[5] == "lane-x"
    assert run.bodies == ["hello\nthere"]


def test_ledger_status_writes_one_status_row_of_the_pinned_lane_and_ticket() -> None:
    run = Recorder()
    assert _call("ledger_status", run, text="pushed the branch", pr="repo#3").ok
    argv = run.calls[0]
    assert argv[:7] == [
        "python3",
        "/p/plugins/omni/scripts/onex_ledger.py",
        "row",
        "STATUS",
        "--lane",
        "lane-x",
        "--ticket",
    ]
    assert "OMN-25178" in argv
    assert "src=codex-mcp-tool" in argv
    assert "pr=repo#3" in argv
    assert argv[-2:] == ["--text", "pushed the branch"]


def test_pr_handoff_check_writes_no_row() -> None:
    run = Recorder(code=4, out="NOT-READY isDraft=true")
    result = _call("pr_handoff_check", run, pr="repo#3", head="abcdef1")
    argv = run.calls[0]
    assert argv[:2] == [
        "bash",
        "/p/plugins/omni/skills/pr-handoff/scripts/handoff_row.sh",
    ]
    assert "--msg-only" in argv
    assert argv[argv.index("--expect-head") + 1] == "abcdef1"
    assert not result.ok
    assert result.exit_code == 4
    assert "NOT-READY" in result.output


def test_refusals_run_nothing() -> None:
    run = Recorder()
    for tool, arguments in (
        ("scm_pr_view", {"target": "not a pr"}),
        ("scm_pr_comment", {"target": "repo#1", "body": " "}),
        ("ledger_status", {"text": "a | b"}),
        ("ledger_status", {"text": "line\nbreak"}),
        ("ledger_status", {"text": "ok", "pr": "nope"}),
        ("pr_handoff_check", {"pr": "repo#1", "head": "zz"}),
        ("scm_pr_view", {"target": "repo#1", "lane": "other"}),
        ("shell", {"command": "id"}),
    ):
        result = _call(tool, run, **arguments)
        assert not result.ok
        assert result.exit_code == 64
        assert result.output.startswith("refused:"), (tool, arguments)
    assert run.calls == []


def test_a_server_with_no_lane_refuses() -> None:
    result = HandlerCodexLaneMcpServerEffect({}, Recorder()).handle(
        ModelCodexLaneMcpToolCall(tool="scm_pr_view", arguments={"target": "repo#1"})
    )
    assert not result.ok
    assert "ONEX_LANE" in result.output
