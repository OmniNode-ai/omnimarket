# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20105 -- the dispatch wire shape: a plain unit must validate against the
pre-``code_draft`` request shape (a pool host still running the old model with
``extra="forbid"`` refuses a null ``code_draft``), while a ``code_draft`` unit
keeps the field on the wire."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost, load_lab_work_topics
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabCodeDraft,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
)

pytestmark = pytest.mark.unit

SHA = "4c3985a451847571e2cebd06a6edf1940069946e"


class _PreviousRequest(BaseModel):
    """The request shape before ``code_draft`` existed: ``extra="forbid"`` so a
    payload that still carries a null ``code_draft`` is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    work_unit_id: str
    target_host: str
    repo: str
    commit_sha: str
    argv: list[str]
    kind: Literal["test", "build", "lint", "code_draft", "other"]
    timeout_seconds: int
    lane: str
    need_tools: list[str]


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=32,
            load1=2.0,
            mem_available_bytes=32 * 1024**3,
            tools=("git", "uv"),
        )


class _Runner:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        self.ran.append(request.work_unit_id)
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=host_name,
            repo=request.repo,
            commit_sha=request.commit_sha,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=3,
            log_path=f"/lab-run/logs/{request.work_unit_id}.log",
            output_tail="1 failed, 41 passed",
            duration_seconds=1.0,
        )


def _request(host: str, **overrides: object) -> ModelLabWorkUnitRequest:
    fields: dict[str, object] = {
        "work_unit_id": "lw-0123456789abcdef",
        "target_host": host,
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": SHA,
        "argv": ["uv", "run", "pytest", "tests/unit", "-q"],
        "kind": "test",
        "timeout_seconds": 60,
        "lane": "test-lane",
        "need_tools": ["git", "uv"],
    }
    fields.update(overrides)
    return ModelLabWorkUnitRequest.model_validate(fields)


def _draft() -> ModelLabCodeDraft:
    return ModelLabCodeDraft(
        task="draft a fix",
        writable_globs=("src/omnimarket/**",),
        checks=(
            {
                "name": "collect",
                "argv": ["uv", "run", "--frozen", "pytest", "-q", "--no-cov"],
            },
        ),
        max_turns=3,
        backend_id="lab",
        max_diff_bytes=1024,
    )


def test_golden_unit_payload_validates_against_the_previous_request_shape() -> None:
    async def scenario() -> list[dict[str, object]]:
        bus = EventBusInmemory(environment="local", group="wire-golden")
        await bus.start()
        topics = load_lab_work_topics()
        seen: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            seen.append(json.loads(message.value)["payload"])

        await bus.subscribe(
            topics.command, on_message=on_command, group_id="wire-probe"
        )
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h202", _Runner()),
            HandlerHostCapacityAdvertiseEffect(_Reader()),
        )
        await host.start()
        caller = LabWorkCaller(bus, wait_slack_seconds=5)
        await caller.start()
        await host.advertise_once()
        await caller.dispatch(_request("h202"))
        await caller.stop()
        await host.stop()
        await bus.close()
        return seen

    seen = asyncio.run(scenario())
    assert len(seen) == 1
    payload = seen[0]
    _PreviousRequest.model_validate(payload)
    assert "code_draft" not in payload


def test_error_unit_with_a_code_draft_keeps_the_field_on_the_wire() -> None:
    async def scenario() -> dict[str, object]:
        bus = EventBusInmemory(environment="local", group="wire-draft")
        await bus.start()
        topics = load_lab_work_topics()
        seen: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            seen.append(json.loads(message.value)["payload"])

        await bus.subscribe(
            topics.command, on_message=on_command, group_id="wire-probe"
        )
        caller = LabWorkCaller(bus, wait_slack_seconds=1)
        await caller.start()
        request = _request(
            "h202",
            kind="code_draft",
            argv=["onex", "code-edit", "run"],
            code_draft=_draft(),
        )
        try:
            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(caller.dispatch(request), timeout=2)
        finally:
            await caller.stop()
            await bus.close()
        return seen[0]

    payload = asyncio.run(scenario())
    assert payload["code_draft"] is not None
    with pytest.raises(ValidationError):
        _PreviousRequest.model_validate(payload)
