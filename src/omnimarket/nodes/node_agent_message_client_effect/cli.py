# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex agent-message``: send and read agent messages, and serve them as MCP tools.

A shell over the agent message client handlers. It parses arguments, builds the runtime and
prints the typed result as plain text, or as JSON with ``--json``. A refusal prints one line on
stderr and exits non-zero with no stack trace: 2 for a request to fix, 3 for a broker or node problem.

Command line::

    onex agent-message send --to codex:review --thread t-1 --kind question \\
      --platform claude --text "please review the diff"
    onex agent-message send --to some-lane --thread t-1 --kind answer --platform codex --text -@answer.txt
    onex agent-message read --to codex:review --thread t-1 --since 2026-11-23T13:00:00Z --limit 20 --json

``--to`` is a lane name or ``platform:name``; ``--kind`` is one of message, question, answer, notice;
``--platform`` is one of claude, codex, glm, hermes, gemini, other. ``--text`` takes the text, ``-@FILE``
to read a file, or ``-`` for stdin. The sender defaults to ``$ONEX_LANE``, then
``$ONEX_AGENT_IDENTITY``, then ``<platform>:<platform>``; ``--from`` overrides it, and the client
sets ``sent_at`` in UTC. The broker is the declared bus lane: ``--bus-lane dev`` (or
``$ONEX_AGENT_MESSAGE_BUS_LANE``) or ``--kafka-bootstrap``; ``--bus inmemory`` is the offline bus.
Refusals: missing_recipient, missing_thread, empty_text, unknown_platform, unknown_kind,
bad_since, bad_limit (exit 2); undeclared_broker, broker_refused, node_unavailable (exit 3).

MCP server: ``onex agent-message serve-mcp --bus-lane dev`` serves two tools over stdio,
``send_agent_message(to, thread, kind, platform, text)`` and
``read_agent_messages(to, thread?, since?, limit?)``. ``kind`` and ``platform`` carry their closed
value lists in the tool schema; a refusal comes back as a tool error with the same text. Register
it as a stdio server whose command is ``onex`` and arguments ``agent-message serve-mcp --bus-lane
dev``, with ``ONEX_LANE`` (or ``ONEX_AGENT_IDENTITY``) in its environment so the sender is named and
``OMNIBASE_PATH`` (the workspace root holding the lane declaration) so the server finds the bus lane; without it a
read is refused ``broker_refused``:

- Claude Code: ``claude mcp add agent-message -e ONEX_LANE=<lane> -e OMNIBASE_PATH=<workspace root> -- onex agent-message serve-mcp --bus-lane dev``
- Codex: a ``[mcp_servers.agent-message]`` table in ``~/.codex/config.toml`` with
  ``command = "onex"`` and ``args = ["agent-message", "serve-mcp", "--bus-lane", "dev"]``.
- Gemini CLI: an entry under ``mcpServers`` in ``~/.gemini/settings.json`` with the same command and args.
- Hermes: the same command and arguments in its MCP server list.

A platform without MCP calls the command line from a shell.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from omnimarket.nodes.node_agent_message_client_effect.handlers.handler_agent_message_send import (
    kind_names,
    platform_names,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message_request import (
    ModelAgentMessageReadRequest,
    ModelAgentMessageSendRequest,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    AgentMessageRefusalError,
)
from omnimarket.nodes.node_agent_message_client_effect.render import (
    render_read,
    render_refusal,
    render_sent,
)
from omnimarket.nodes.node_agent_message_client_effect.runtime import (
    AgentMessageRuntime,
    BusSettings,
)

_STDIN = "-"
_FILE_PREFIX = "-@"


def read_text_argument(value: str) -> str:
    """``-@<path>`` reads the text from a file, ``-`` from stdin; anything else is the text."""
    if value == _STDIN:
        return sys.stdin.read()
    if value.startswith(_FILE_PREFIX):
        path = Path(value[len(_FILE_PREFIX) :])
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise click.UsageError(f"cannot read the text file {path}: {exc}") from None
    return value


def _runtime(ctx: click.Context) -> AgentMessageRuntime:
    if isinstance(ctx.obj, AgentMessageRuntime):
        return ctx.obj
    from omnimarket.nodes.node_agent_message_client_effect.adapter_bus_node import (
        bus_runtime,
    )

    settings = ctx.meta.get("agent_message_bus")
    return bus_runtime(settings if isinstance(settings, BusSettings) else BusSettings())


def _refuse(refusal: AgentMessageRefusalError, *, as_json: bool) -> None:
    click.echo(render_refusal(refusal, as_json=as_json), err=True)
    sys.exit(refusal.exit_code)


_bus_options = [
    click.option(
        "--omnibase-path",
        envvar="OMNIBASE_PATH",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Workspace root holding the lane declaration. Bound to $OMNIBASE_PATH.",
    ),
    click.option(
        "--bus",
        type=click.Choice(["kafka", "inmemory"]),
        default="kafka",
        show_default=True,
    ),
    click.option(
        "--bus-lane",
        envvar="ONEX_AGENT_MESSAGE_BUS_LANE",
        default=None,
        help="The declared bus lane to use, e.g. dev.",
    ),
    click.option(
        "--kafka-bootstrap", default=None, help="The broker, stated directly."
    ),
]


def with_bus_options(func: click.decorators.FC) -> click.decorators.FC:
    for option in reversed(_bus_options):
        func = option(func)
    return func


def _remember_bus(
    ctx: click.Context,
    *,
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
) -> None:
    ctx.meta["agent_message_bus"] = BusSettings(
        bus=bus,
        bus_lane=bus_lane,
        kafka_bootstrap=kafka_bootstrap,
        omnibase_path=omnibase_path,
    )


@click.group("agent-message")
@click.pass_context
def agent_message_group(ctx: click.Context) -> None:
    """Send and read messages between agents on any platform, over the event bus."""
    ctx.meta.setdefault("agent_message_bus", BusSettings())


@agent_message_group.command("send")
@click.option("--to", "to", default="", help="Recipient: a lane name or platform:name.")
@click.option("--thread", default="", help="Conversation id the message belongs to.")
@click.option("--kind", default="", help=f"Message kind: one of {kind_names()}.")
@click.option(
    "--platform", default="", help=f"The sender's platform: one of {platform_names()}."
)
@click.option(
    "--text",
    default="",
    help="Message text; -@FILE reads it from a file, - from stdin.",
)
@click.option(
    "--from",
    "sender",
    default=None,
    help="Sender; defaults to $ONEX_LANE or $ONEX_AGENT_IDENTITY.",
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@with_bus_options
@click.pass_context
def send_command(
    ctx: click.Context,
    to: str,
    thread: str,
    kind: str,
    platform: str,
    text: str,
    sender: str | None,
    as_json: bool,
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
) -> None:
    """Send one message to a lane or platform:name on a thread."""
    _remember_bus(
        ctx,
        omnibase_path=omnibase_path,
        bus=bus,
        bus_lane=bus_lane,
        kafka_bootstrap=kafka_bootstrap,
    )
    request = ModelAgentMessageSendRequest(
        to=to,
        thread=thread,
        kind=kind,
        platform=platform,
        text=read_text_argument(text) if text else "",
        sender=sender,
    )
    try:
        result = _runtime(ctx).send(request)
    except AgentMessageRefusalError as refusal:
        _refuse(refusal, as_json=as_json)
        return
    click.echo(render_sent(result.message, as_json=as_json))


@agent_message_group.command("read")
@click.option("--to", "to", default="", help="Recipient whose messages to read.")
@click.option("--thread", default=None, help="Only this conversation.")
@click.option(
    "--since", default=None, help="Only messages sent at or after this ISO-8601 time."
)
@click.option(
    "--limit",
    default=50,
    show_default=True,
    type=int,
    help="At most this many, newest kept.",
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@with_bus_options
@click.pass_context
def read_command(
    ctx: click.Context,
    to: str,
    thread: str | None,
    since: str | None,
    limit: int,
    as_json: bool,
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
) -> None:
    """Read the messages addressed to a recipient, oldest first."""
    _remember_bus(
        ctx,
        omnibase_path=omnibase_path,
        bus=bus,
        bus_lane=bus_lane,
        kafka_bootstrap=kafka_bootstrap,
    )
    request = ModelAgentMessageReadRequest(
        to=to, thread=thread, since=since, limit=limit
    )
    try:
        result = _runtime(ctx).read(request)
    except AgentMessageRefusalError as refusal:
        _refuse(refusal, as_json=as_json)
        return
    click.echo(render_read(result, as_json=as_json))


@agent_message_group.command("serve-mcp")
@with_bus_options
@click.pass_context
def serve_mcp_command(
    ctx: click.Context,
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
) -> None:
    """Serve send_agent_message and read_agent_messages as MCP tools over stdio."""
    _remember_bus(
        ctx,
        omnibase_path=omnibase_path,
        bus=bus,
        bus_lane=bus_lane,
        kafka_bootstrap=kafka_bootstrap,
    )
    from omnimarket.nodes.node_agent_message_client_effect.mcp_server import (
        build_server,
    )

    build_server(_runtime(ctx)).run(transport="stdio")
