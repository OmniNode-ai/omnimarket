# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four onex tools a Codex lane calls over MCP (OMN-25178).

A Codex lane runs ``codex exec`` in a sandbox with no Claude Code skill and no hook, so it could not call ``onex scm``, the ledger
write or the handoff check. The remote-lane runner writes the lane a Codex config whose ``mcp_servers`` table starts this node's
server (``python -m omnimarket.nodes.node_codex_lane_mcp_server_effect``); Codex starts it itself, outside the shell sandbox, so
each tool runs the same command the lane's own shell would, as the same host user.

The tools are a fixed list. Every tool is one argv with no shell. The lane name, the ticket, the ledger and the plugin root come
from the server's environment (the runner writes them), never from the tool's arguments: a lane cannot write another lane's row.

* ``scm_pr_view(target)``: ``onex scm pr-view <target>`` (the watcher's state first, then the bus).
* ``scm_pr_comment(target, body)``: ``onex scm pr-comment <target> --lane <lane>``.
* ``ledger_status(text, pr)``: one STATUS row of the lane, through the plugin's ``onex_ledger.py row``.
* ``pr_handoff_check(pr, head)``: the pr-handoff readiness read (``handoff_row.sh ... --msg-only``), which writes no row.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from omnimarket.nodes.node_codex_lane_mcp_server_effect.models.model_codex_lane_mcp_tool import (
    ModelCodexLaneMcpToolCall,
    ModelCodexLaneMcpToolResult,
)

CODEX_LANE_MCP_TOOLS = (
    "scm_pr_view",
    "scm_pr_comment",
    "ledger_status",
    "pr_handoff_check",
)
SCM_BUS_LANE = "dev"  # the bus lane whose runtime serves the source-control nodes, as the remote-lane runner uses it
PR_TARGET_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+#[0-9]+$")
HEAD_RE = re.compile(r"^[0-9a-f]{7,40}$")
MAX_TEXT = 4000
MAX_BODY = 20000
OUTPUT_TAIL = 6000
TIMEOUT_S = 540


class HandlerCodexLaneMcpServerEffect:
    """Runs one tool call as one command; ``run`` is injectable so a test never starts a process."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._env = dict(os.environ if environ is None else environ)
        self._run = run

    def _need(self, name: str) -> str:
        value = self._env.get(name, "").strip()
        if not value:
            raise ValueError(f"the server's environment has no {name}")
        return value

    def _onex(self) -> str:
        wrapper = (
            Path(self._env.get("OMNI_HOME", "")) / "omnibase_infra" / "scripts" / "onex"
        )
        return str(wrapper) if wrapper.is_file() else "onex"

    def _argv(self, request: ModelCodexLaneMcpToolCall, body_file: str) -> list[str]:
        """The one command for a tool call, or ValueError naming what the arguments got wrong."""
        arguments = request.arguments
        lane = self._need("ONEX_LANE")
        unknown = set(arguments) - {
            "scm_pr_view": {"target"},
            "scm_pr_comment": {"target", "body"},
            "ledger_status": {"text", "pr"},
            "pr_handoff_check": {"pr", "head"},
        }.get(request.tool, set())
        if request.tool not in CODEX_LANE_MCP_TOOLS or unknown:
            raise ValueError(
                f"unknown tool {request.tool!r} or arguments {sorted(unknown)}; the tools are {list(CODEX_LANE_MCP_TOOLS)}"
            )
        target = arguments.get("target") or arguments.get("pr", "")
        if request.tool != "ledger_status" and not PR_TARGET_RE.match(target):
            raise ValueError("the pull request is <owner>/<repo>#<n> or <repo>#<n>")
        if request.tool == "scm_pr_view":
            return [self._onex(), "scm", "pr-view", target, "--bus-lane", SCM_BUS_LANE]
        if request.tool == "scm_pr_comment":
            body = arguments.get("body", "")
            if not body.strip() or len(body) > MAX_BODY:
                raise ValueError(f"body must be 1 to {MAX_BODY} characters")
            Path(body_file).write_text(body, encoding="utf-8")
            return [
                self._onex(),
                "scm",
                "pr-comment",
                target,
                "--lane",
                lane,
                "--bus-lane",
                SCM_BUS_LANE,
                "--body-file",
                body_file,
            ]
        if request.tool == "ledger_status":
            return self._status_argv(
                lane, arguments.get("text", ""), arguments.get("pr", "")
            )
        head = arguments.get("head", "")
        if head and not HEAD_RE.match(head):
            raise ValueError("head is a commit sha (7 to 40 hex digits)")
        plugin = self._need("OMNI_PLUGIN_ROOT")
        return [
            "bash",
            f"{plugin}/skills/pr-handoff/scripts/handoff_row.sh",
            target,
            "land",
            lane,
            "--msg-only",
            *(["--expect-head", head] if head else []),
        ]

    def _status_argv(self, lane: str, text: str, pr: str) -> list[str]:
        if (
            not text.strip()
            or len(text) > MAX_TEXT
            or "|" in text
            or "\n" in text
            or "\r" in text
        ):
            raise ValueError(
                f"text is one line of 1 to {MAX_TEXT} characters with no pipe"
            )
        if pr and not PR_TARGET_RE.match(pr):
            raise ValueError("pr is <owner>/<repo>#<n> or <repo>#<n>")
        plugin = self._need("OMNI_PLUGIN_ROOT")
        ticket = self._env.get("ONEX_LANE_TICKET", "").strip()
        return [
            "python3",
            f"{plugin}/scripts/onex_ledger.py",
            "row",
            "STATUS",
            "--lane",
            lane,
            *(["--ticket", ticket] if ticket else []),
            "--kv",
            "src=codex-mcp-tool",
            *(["--kv", f"pr={pr}"] if pr else []),
            "--text",
            text,
        ]

    def handle(self, request: ModelCodexLaneMcpToolCall) -> ModelCodexLaneMcpToolResult:
        with tempfile.TemporaryDirectory(prefix="codex-lane-mcp-") as scratch:
            try:
                argv = self._argv(request, str(Path(scratch) / "body.md"))
            except ValueError as refusal:
                return ModelCodexLaneMcpToolResult(
                    ok=False, exit_code=64, output=f"refused: {refusal}"
                )
            env = {
                **self._env,
                "PR_HANDOFF_HEAD_WAIT_S": self._env.get("PR_HANDOFF_HEAD_WAIT_S", "30"),
            }
            try:
                done = self._run(
                    argv,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return ModelCodexLaneMcpToolResult(
                    ok=False, exit_code=124, output=f"timed out after {TIMEOUT_S}s"
                )
            except OSError as exc:
                return ModelCodexLaneMcpToolResult(
                    ok=False,
                    exit_code=127,
                    output=f"could not start the command: {exc.strerror}",
                )
        text = "\n".join(part for part in (done.stdout, done.stderr) if part)[
            -OUTPUT_TAIL:
        ]
        return ModelCodexLaneMcpToolResult(
            ok=done.returncode == 0, exit_code=done.returncode, output=text
        )
