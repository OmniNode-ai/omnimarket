# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve the lane tools over MCP on stdio: ``python -m omnimarket.nodes.node_codex_lane_mcp_server_effect``.

Codex starts this from the ``mcp_servers`` table the remote-lane runner wrote for the lane. Every tool returns the command's exit
code and output tail as one JSON line; a refusal or failure is a normal result with ``ok`` false, never a server error.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from omnimarket.nodes.node_codex_lane_mcp_server_effect.handlers import (
    HandlerCodexLaneMcpServerEffect,
)
from omnimarket.nodes.node_codex_lane_mcp_server_effect.models import (
    ModelCodexLaneMcpToolCall,
)


def build_server(handler: HandlerCodexLaneMcpServerEffect | None = None) -> MCPServer:
    handler = handler or HandlerCodexLaneMcpServerEffect()
    server = MCPServer("onex_lane")

    def call(tool: str, **arguments: str) -> str:
        return handler.handle(
            ModelCodexLaneMcpToolCall(
                tool=tool, arguments={k: v for k, v in arguments.items() if v}
            )
        ).model_dump_json()

    @server.tool()
    def scm_pr_view(target: str) -> str:
        """Read one pull request (`<owner>/<repo>#<n>`): state, head, draft, CI class. Replaces `gh pr view`."""
        return call("scm_pr_view", target=target)

    @server.tool()
    def scm_pr_comment(target: str, body: str) -> str:
        """Comment on a pull request this lane owns (`<owner>/<repo>#<n>`). Replaces `gh pr comment`."""
        return call("scm_pr_comment", target=target, body=body)

    @server.tool()
    def ledger_status(text: str, pr: str = "") -> str:
        """Append one STATUS row of this lane to the rolling ledger: one line, no pipe; `pr` optionally names `<repo>#<n>`."""
        return call("ledger_status", text=text, pr=pr)

    @server.tool()
    def pr_handoff_check(pr: str, head: str = "") -> str:
        """Check a pull request's handoff readiness (`<repo>#<n>`, `head` the pushed sha). Writes no row; you never hand off."""
        return call("pr_handoff_check", pr=pr, head=head)

    return server


if __name__ == "__main__":
    build_server().run("stdio")
