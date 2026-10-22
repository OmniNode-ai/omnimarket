# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Agent-session transport owned by the delegation call effect.

Lab work invokes this file with the standard-library interpreter in its
checkout. Brief chunks stay below the host's argv bound and are joined on
stdin here, at the model-call boundary. Output and the agent's exit status
pass through to the lab-work receipt.

A tools-only session (RULING 2026-10-21T22:32:15Z, OMN-17427) is the agent
transport for a caller that gives its agent specific, deterministic tools and
nothing else: every built-in tool is off (no shell, no file tools, no web, no
subagent), the caller's one MCP server is the only tool source, and permission
checks stay on with that server's tools as the one grant. The session is built
and refused here, at the model-call boundary, so no caller can widen it.
"""

from __future__ import annotations

import subprocess
import sys

#: Flags that would hand a tools-only agent more than its server's tools.
TOOLS_ONLY_FORBIDDEN_FLAGS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--add-dir",
        "--plugin-dir",
        "--agents",
        "--settings",
        "--disallowedTools",
        "--disallowed-tools",
        "--allowed-tools",
    }
)
TOOLS_ONLY_TIMEOUT_S = 3600


class AgentSessionRefusedError(ValueError):
    """A tools-only session would hold more than its one tool server's tools."""


def _flag_value(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    index = argv.index(flag)
    return argv[index + 1] if index + 1 < len(argv) else None


def tools_only_server(mcp_config: dict[str, object]) -> str:
    """The name of the session's one tool server; anything else refuses."""
    servers = mcp_config.get("mcpServers")
    if not isinstance(servers, dict) or len(servers) != 1:
        raise AgentSessionRefusedError(
            "a tools-only session takes exactly one MCP server"
        )
    (name,) = servers
    if not isinstance(name, str) or not name.isidentifier():
        raise AgentSessionRefusedError(f"not a server name: {name!r}")
    return name


def tools_only_argv(model: str, mcp_config: str, server: str, schema: str) -> list[str]:
    """Built-in tools off, one tool server, checks on, a typed result."""
    return [
        "claude",
        "-p",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config,
        "--allowedTools",
        f"mcp__{server}",
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "",
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--model",
        model,
    ]


def refuse_unless_tools_only(argv: list[str], server: str) -> None:
    """Raise unless the session holds only ``server``'s tools."""
    problems: list[str] = []
    if _flag_value(argv, "--tools") != "":
        problems.append("built-in tools are not all off (--tools must be empty)")
    if "--strict-mcp-config" not in argv:
        problems.append(
            "--strict-mcp-config is missing, so other MCP servers would load"
        )
    if argv.count("--allowedTools") != 1 or _flag_value(argv, "--allowedTools") != (
        f"mcp__{server}"
    ):
        problems.append(f"the one grant must be mcp__{server}")
    if _flag_value(argv, "--permission-mode") != "dontAsk":
        problems.append("the permission mode must be dontAsk")
    if _flag_value(argv, "--setting-sources") != "":
        problems.append("user, project and local settings must not load")
    problems += [
        f"forbidden flag {f}" for f in sorted(TOOLS_ONLY_FORBIDDEN_FLAGS) if f in argv
    ]
    if problems:
        raise AgentSessionRefusedError(
            "refused a tools-only session: " + "; ".join(problems)
        )


class HandlerAgentSession:
    """Run one delegated agent session in the lab unit's checkout."""

    def run(self, model: str, brief: str) -> int:
        """Keep the agent's permission checks and deliver the brief on stdin."""
        done = subprocess.run(
            [
                "claude",
                "-p",
                "--permission-mode",
                "acceptEdits",
                "--output-format",
                "text",
                "--model",
                model,
            ],
            input=brief,
            text=True,
            check=False,
        )
        return done.returncode

    def run_tools_only(
        self, model: str, brief: str, mcp_config: str, schema: str
    ) -> subprocess.CompletedProcess[str]:
        """One tools-only session; its JSON result is the caller's to read."""
        # Keep top-level imports to subprocess and sys, the deploy probe's stdlib set.
        import json

        with open(mcp_config, encoding="utf-8") as config_file:
            loaded = json.loads(config_file.read())
        if not isinstance(loaded, dict):
            raise AgentSessionRefusedError("the MCP config must be one JSON object")
        server = tools_only_server(loaded)
        with open(schema, encoding="utf-8") as schema_file:
            schema_text = schema_file.read()
        json.loads(schema_text)
        argv = tools_only_argv(model, mcp_config, server, schema_text)
        refuse_unless_tools_only(argv, server)
        return subprocess.run(
            argv,
            input=brief,
            text=True,
            capture_output=True,
            timeout=TOOLS_ONLY_TIMEOUT_S,
            check=False,
        )


def main_tools_only(argv: list[str]) -> int:
    """``--tools-only <model> <mcp config> <json schema>``; the brief is stdin."""
    if len(argv) != 3:
        sys.stderr.write(
            "usage: handler_agent_session.py --tools-only <model> <mcp-config> <schema>\n"
        )
        return 64
    try:
        done = HandlerAgentSession().run_tools_only(
            argv[0], sys.stdin.read(), argv[1], argv[2]
        )
    except (AgentSessionRefusedError, ValueError, OSError) as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 65
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    return done.returncode


def main(argv: list[str]) -> int:
    """The standalone effect entrypoint for a pool host without a venv."""
    if argv[:1] == ["--tools-only"]:
        return main_tools_only(argv[1:])
    if len(argv) < 2:
        sys.stderr.write("usage: handler_agent_session.py <model> <brief chunk>...\n")
        return 64
    return HandlerAgentSession().run(argv[0], "".join(argv[1:]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
