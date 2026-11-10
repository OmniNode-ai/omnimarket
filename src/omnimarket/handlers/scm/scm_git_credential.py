# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm git-credential`` -- a git credential helper over the git credential node (OMN-22613).

Configure git with::

    git config credential.https://github.com.helper '!onex scm git-credential'
    git config credential.https://github.com.useHttpPath true

git then runs ``onex scm git-credential get`` and writes ``protocol``, ``host``
and ``path`` lines on stdin. For the overlay's ``git_host`` and a path naming
OWNER/NAME, the helper makes a one-request X25519 key pair in memory, publishes
node_github_git_credential_effect's request with the public half, waits for the
node's terminal, opens the sealed token and writes ``username`` and ``password``
to stdout, which only git reads. Nothing is written to disk, and ``store`` and
``erase`` do nothing: the token lives one hour and is asked for again per push.

Any other host gets no answer (git falls through to its next helper). A missing
path is refused naming ``useHttpPath``, since the token is per repository. A
refusal from the node exits 1 with the node's typed reason on stderr; no
terminal in time exits 75; a bus that cannot be opened exits 69.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from importlib import resources
from pathlib import Path
from typing import Final, Protocol

import click
import yaml
from omnibase_infra.errors import InfraConnectionError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.github_credential_seal import RecipientKeyPair, SealError
from omnimarket.models.scm import ModelScmTopics
from omnimarket.nodes.node_github_git_credential_effect.handlers.handler_github_git_credential import (
    NODE_NAME,
    resolve_policy,
    split_repository,
)
from omnimarket.nodes.node_github_git_credential_effect.models import (
    GIT_CREDENTIAL_USERNAME,
    ModelGitCredentialIssued,
    ModelGitCredentialRequest,
)

EXIT_REFUSED: Final[int] = 1
EXIT_BUS_UNAVAILABLE: Final[int] = 69
EXIT_NO_TERMINAL: Final[int] = 75


class GitCredentialRefusedError(RuntimeError):
    """The node answered failed, or its answer did not open."""


class ProtocolGitCredentialAsker(Protocol):
    """Asks for one repository's push token and returns it in clear, in memory only."""

    async def ask(self, repository: str) -> str: ...


def node_topics() -> ModelScmTopics:
    """The node's command and terminal topics, as its contract declares them."""
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{NODE_NAME}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    dispatch = contract["runtime_dispatch"]
    return ModelScmTopics(
        command=str(dispatch["command_topic"]),
        success=str(dispatch["terminal_events"]["success"]),
        failure=str(dispatch["terminal_events"]["failure"]),
    )


class BusGitCredentialAsker:
    """Publishes the node's request on the bus and opens the sealed answer."""

    def __init__(
        self,
        open_bus: Callable[
            [], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]
        ],
        *,
        timeout_s: float,
        lane: str | None,
        host: str | None,
    ) -> None:
        self._open_bus = open_bus
        self._timeout_s = timeout_s
        self._lane = lane
        self._host = host

    async def ask(self, repository: str) -> str:
        keys = RecipientKeyPair.generate()
        correlation_id = uuid.uuid4()
        request = ModelGitCredentialRequest(
            correlation_id=correlation_id,
            repository=repository,
            recipient_public_key=keys.public_text,
            requested_by_lane=self._lane,
            requested_by_host=self._host,
        )
        async with self._open_bus() as bus:
            terminal = await ScmBusCaller(bus).call_topics(
                topics=node_topics(),
                request=request,
                correlation_id=correlation_id,
                timeout_s=self._timeout_s,
            )
        if terminal.failed:
            reason = terminal.payload.get("reason", "unknown")
            detail = terminal.payload.get("detail", "")
            raise GitCredentialRefusedError(f"{reason}: {detail}")
        issued = ModelGitCredentialIssued.model_validate(terminal.payload)
        try:
            return keys.open(
                issued.sealed_token,
                correlation_id=correlation_id,
                repository=repository,
            )
        except SealError as exc:
            raise GitCredentialRefusedError(f"the answer did not open: {exc}") from None


def parse_credential_input(text: str) -> dict[str, str]:
    """git's ``key=value`` lines, up to the first blank line."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.rstrip("\r")
    return fields


async def answer_git_credential(
    action: str, stdin_text: str, asker: ProtocolGitCredentialAsker, *, git_host: str
) -> str:
    """What the helper writes to stdout for one git call; raises on a refusal."""
    if action != "get":
        return ""
    fields = parse_credential_input(stdin_text)
    if fields.get("protocol") != "https" or fields.get("host", "").lower() != git_host:
        return ""
    path = fields.get("path", "").strip("/")
    if not path:
        raise GitCredentialRefusedError(
            "git sent no path, and the token is per repository: set "
            f"credential.https://{git_host}.useHttpPath true"
        )
    if split_repository(path) is None:
        raise GitCredentialRefusedError(f"path {path!r} does not name OWNER/NAME")
    token = await asker.ask(path.removesuffix(".git"))
    return f"username={GIT_CREDENTIAL_USERNAME}\npassword={token}\n"


def _bus_opener(
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
) -> Callable[[], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]]:
    if bus == "inmemory":
        raise LabRunBusError(
            f"{NODE_NAME} runs in the runtime that holds the App credential; "
            "the helper never hosts it in this process"
        )
    lane = bus_lane
    if lane is None and kafka_bootstrap is None:
        from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
            resolve_node_event_bus,
        )

        declared, path = resolve_node_event_bus(NODE_NAME, omnibase_path)
        if declared is None or declared.type != "kafka":
            raise LabRunBusError(
                f"{NODE_NAME} declares no kafka event_bus in its contract overlay "
                f"({path}), and no --bus-lane or --kafka-bootstrap was given"
            )
        lane = declared.lane

    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[ProtocolLabRunBus]:
        from omnimarket.handlers.scm.scm_cli import _open_bus

        async with _open_bus(
            {
                "bus": "kafka",
                "bus_lane": lane,
                "kafka_bootstrap": kafka_bootstrap,
                "omnibase_path": omnibase_path,
                "scm_node": None,
            }
        ) as live:
            yield live

    return opened


@click.command("git-credential")
@_with_bus_options
@click.argument("action", type=click.Choice(["get", "store", "erase"]))
@click.option("--timeout-s", type=float, default=60.0, show_default=True)
def git_credential_command(
    action: str,
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    timeout_s: float,
) -> None:
    """git credential helper: a per-repository push token from the git credential node."""
    stdin_text = sys.stdin.read()
    if action != "get":
        return
    policy = resolve_policy()
    try:
        asker = BusGitCredentialAsker(
            _bus_opener(omnibase_path, bus, bus_lane, kafka_bootstrap),
            timeout_s=timeout_s,
            lane=os.environ.get("ONEX_LANE") or None,
            host=socket.gethostname(),
        )
        answer = asyncio.run(
            answer_git_credential(
                action, stdin_text, asker, git_host=policy.git_host.lower()
            )
        )
    except GitCredentialRefusedError as exc:
        click.echo(f"onex scm git-credential: refused: {exc}", err=True)
        sys.exit(EXIT_REFUSED)
    except (LabRunBusError, InfraConnectionError) as exc:
        click.echo(f"onex scm git-credential: bus unavailable: {exc}", err=True)
        sys.exit(EXIT_BUS_UNAVAILABLE)
    except TimeoutError:
        click.echo(
            f"onex scm git-credential: {NODE_NAME} answered nothing in {timeout_s}s",
            err=True,
        )
        sys.exit(EXIT_NO_TERMINAL)
    click.echo(answer, nl=False)


__all__ = [
    "EXIT_BUS_UNAVAILABLE",
    "EXIT_NO_TERMINAL",
    "EXIT_REFUSED",
    "BusGitCredentialAsker",
    "GitCredentialRefusedError",
    "ProtocolGitCredentialAsker",
    "answer_git_credential",
    "git_credential_command",
    "node_topics",
    "parse_credential_input",
]
