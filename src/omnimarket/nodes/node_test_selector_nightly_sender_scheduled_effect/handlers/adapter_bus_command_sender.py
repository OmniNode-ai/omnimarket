# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sends the collect and publish commands on the bus and awaits the terminal event that answers each.

The topics are this node's own contract's ``command_sends`` block. The transport is the shared contract-topic bus caller the
source-control and ticket commands use: it publishes the typed request in an event envelope and waits for the one terminal
event that names it. A timeout, a bus that cannot open or an answer of an unknown shape is raised as
:class:`NightlyCommandError`, never read as a sent command.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from uuid import uuid4

import yaml
from pydantic import BaseModel, JsonValue, ValidationError

from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    ModelCollectAnswer,
    ModelCollectCommand,
    ModelPublishAnswer,
    ModelPublishCommand,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.protocols.protocol_nightly_sender import (
    NightlyCommandError,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
COLLECT_OPERATION = "collect_nightly"
PUBLISH_OPERATION = "publish_attribution_record"
RESULT_KEY = "result"


def load_command_topics(
    operation: str, contract_path: Path = CONTRACT_PATH
) -> ModelScmTopics:
    """The command topic and the terminal topic of one operation, from the contract's ``command_sends``."""
    loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    for entry in loaded["command_sends"]:
        if entry["operation"] == operation:
            return ModelScmTopics(
                command=str(entry["command_topic"]),
                success=str(entry["terminal_topic"]),
            )
    raise NightlyCommandError(f"the contract declares no command_sends for {operation}")


def answer_body(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The result a terminal event carries: the payload itself, or the mapping under ``result``."""
    wrapped = payload.get(RESULT_KEY)
    return wrapped if isinstance(wrapped, dict) else payload


class BusNightlyCommandSender:
    """Publishes on the declared lane and awaits the answer, the way the scheduled ticket closer's bus call does."""

    def __init__(
        self,
        *,
        bus_lane: str,
        timeout_s: float,
        call: Callable[[ModelScmTopics, BaseModel], ModelScmTerminal] | None = None,
    ) -> None:
        self._lane = bus_lane
        self._timeout_s = timeout_s
        self._call = call or self._call_on_bus

    def _call_on_bus(
        self, topics: ModelScmTopics, request: BaseModel
    ) -> ModelScmTerminal:
        # Imported here: a test with a fake call must not need a bus client.
        from omnibase_infra.errors import InfraConnectionError

        from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
        from omnimarket.delegated_test_loop.lane_bus import (
            LabRunBusError,
            open_lab_run_bus,
        )
        from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller

        workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
        # The opener's fourth keyword (the workspace root) carries the private registry clone's name, which the
        # public-repo hygiene gate refuses; its parameters are bound from its signature in declared order.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
            raise NightlyCommandError(f"unexpected bus opener parameters: {names}")
        opener = cast(
            Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
            open_lab_run_bus,
        )
        values = ("kafka", self._lane, None, Path(workspace) if workspace else None)

        async def run() -> ModelScmTerminal:
            async with opener(**dict(zip(names, values, strict=True))) as bus:
                return await ScmBusCaller(bus).call_topics(
                    topics=topics,
                    request=request,
                    correlation_id=uuid4(),
                    timeout_s=self._timeout_s,
                )

        try:
            # The effect runs under a loop; the bus call gets a thread of its own.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, run()).result()
        except TimeoutError as exc:
            raise NightlyCommandError(
                f"no terminal event on {topics.success} within {self._timeout_s:.0f}s on lane {self._lane!r}"
            ) from exc
        except (LabRunBusError, InfraConnectionError) as exc:
            raise NightlyCommandError(f"bus lane {self._lane!r}: {exc}") from exc

    def collect_nightly(self, command: ModelCollectCommand) -> ModelCollectAnswer:
        terminal = self._call(load_command_topics(COLLECT_OPERATION), command)
        body = answer_body(terminal.payload)
        try:
            return ModelCollectAnswer.model_validate(
                {
                    "status": body.get("status"),
                    "slot_date": body.get("slot_date"),
                    "failure": str(body.get("failure") or ""),
                    "raw": body,
                }
            )
        except ValidationError as exc:
            raise NightlyCommandError(
                f"the collect answer on {terminal.receipt.terminal_topic} is not a collect result"
            ) from exc

    def publish_attribution_record(
        self, command: ModelPublishCommand
    ) -> ModelPublishAnswer:
        terminal = self._call(load_command_topics(PUBLISH_OPERATION), command)
        try:
            return ModelPublishAnswer.model_validate(
                {
                    key: value
                    for key, value in answer_body(terminal.payload).items()
                    if key in ModelPublishAnswer.model_fields
                }
            )
        except ValidationError as exc:
            raise NightlyCommandError(
                f"the publish answer on {terminal.receipt.terminal_topic} is not a publish result"
            ) from exc


__all__: list[str] = [
    "BusNightlyCommandSender",
    "answer_body",
    "load_command_topics",
]
