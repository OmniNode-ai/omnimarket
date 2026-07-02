# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full I/O-boundary EFFECT coverage for node_kb_repowise_index_effect, driven
over the canonical in-memory bus.

OMN-13674 (cluster wave-kb-context-knowledge, archetype effect). This module
drives ``HandlerKBRepoWiseIndex`` end to end over ``EventBusInmemory`` (via the
``integration_event_bus`` fixture + ``LocalRuntimeBusAdapter``): a
``ModelKBRepoIndexRequest`` lands on the declared command topic
``onex.cmd.omnimarket.kb-repowise-index-requested.v1`` and the terminal
``ModelKBRepoIndexResult`` is auto-published onto the declared completed topic
``onex.evt.omnimarket.kb-repowise-index-completed.v1``. No live Kafka / ``.201``.

The subprocess boundary (gh clone / git rev-parse / repowise index) is replaced
by a constructor-injected ``_MockRunner`` (the canonical ``_Mock*`` injection
pattern) — subprocess is NEVER monkeypatched and no real git/gh/repowise ever
runs, so no prod-mutating effect is exercised.

EFFECT DoD covered — every outcome at the injected I/O boundary:
  * dry-run success (no subprocess) → ``success=True``;
  * full success → ``success=True`` with ``commit_sha`` + parsed ``entry_count``;
  * clone failure mode → ``success=False`` with a ``Clone failed`` error;
  * repowise-index failure mode → ``success=False`` with ``commit_sha`` set and a
    ``Repowise index failed`` error;
  * the ``rev-parse`` failure branch → ``commit_sha=None`` on an otherwise
    successful index (the ``_get_commit_sha`` ``except`` path);
  * idempotency: identical input yields an identical terminal event.
Typed result fields are asserted off the terminal event — never a bare
"returned without raising".
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from omnimarket.nodes.node_kb_repowise_index_effect.handlers.handler_kb_repowise_index import (
    HandlerKBRepoWiseIndex,
)
from omnimarket.nodes.node_kb_repowise_index_effect.models.model_index_request import (
    ModelKBRepoIndexRequest,
)
from omnimarket.nodes.node_kb_repowise_index_effect.models.model_index_result import (
    ModelKBRepoIndexResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.kb-repowise-index-requested.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.kb-repowise-index-completed.v1"


class _MockRunner:
    """Constructor-injected subprocess seam — no real git/gh/repowise runs."""

    def __init__(
        self,
        *,
        commit_sha: str | None = "deadbeef1234",
        index_stdout: str = "Indexed 42 documents\n",
        fail_on: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._commit_sha = commit_sha
        self._index_stdout = index_stdout
        self._fail_on = fail_on  # "clone" | "rev-parse" | "index" | None

    @staticmethod
    def _token(cmd: list[str]) -> str:
        for t in ("clone", "rev-parse", "index"):
            if t in cmd:
                return t
        return "other"

    def __call__(
        self, cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        token = self._token(cmd)
        if self._fail_on == token:
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, stderr=f"{token} boom"
            )
        if token == "rev-parse":
            stdout = f"{self._commit_sha}\n" if self._commit_sha else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if token == "index":
            return subprocess.CompletedProcess(
                cmd, 0, stdout=self._index_stdout, stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


async def _drive(
    bus: Any, command: ModelKBRepoIndexRequest, runner: _MockRunner
) -> ModelKBRepoIndexResult:
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerKBRepoWiseIndex(run=runner),
        handler_name="kb-repowise-index",
        input_model_cls=ModelKBRepoIndexRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-kb-repowise-test",
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=command.model_dump_json().encode("utf-8")
    )
    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    assert completed[-1].topic == "onex.evt.omnimarket.kb-repowise-index-completed.v1"
    return ModelKBRepoIndexResult.model_validate(json.loads(completed[-1].value))


# ---------------------------------------------------------------------------
# dry-run: success, no subprocess touched.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dry_run_success_no_subprocess_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        runner = _MockRunner()
        result = await _drive(bus, ModelKBRepoIndexRequest(dry_run=True), runner)
        assert result.success is True
        assert result.commit_sha is None
        assert result.entry_count == 0
        assert result.error is None
        assert runner.calls == []  # the effect boundary was never crossed
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# full success: clone + rev-parse + index all succeed.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_full_success_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        runner = _MockRunner(commit_sha="abc123", index_stdout="Total entries: 17\n")
        result = await _drive(
            bus,
            ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base"),
            runner,
        )
        assert result.success is True
        assert result.commit_sha == "abc123"
        assert result.entry_count == 17
        assert result.error is None
        # The boundary was crossed for clone, rev-parse, and index exactly.
        tokens = [_MockRunner._token(c) for c in runner.calls]
        assert tokens == ["clone", "rev-parse", "index"]
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# clone failure mode.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_clone_failure_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        runner = _MockRunner(fail_on="clone")
        result = await _drive(
            bus, ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base"), runner
        )
        assert result.success is False
        assert result.error is not None
        assert "Clone failed" in result.error
        assert result.commit_sha is None
        assert result.entry_count == 0
        # index was never attempted after the clone failed.
        assert [_MockRunner._token(c) for c in runner.calls] == ["clone"]
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# repowise index failure mode — commit_sha still surfaced.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_index_failure_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        runner = _MockRunner(commit_sha="sha999", fail_on="index")
        result = await _drive(
            bus, ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base"), runner
        )
        assert result.success is False
        assert result.error is not None
        assert "Repowise index failed" in result.error
        assert result.commit_sha == "sha999"
        assert result.entry_count == 0
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# rev-parse failure branch: commit_sha=None on an otherwise successful index.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_commit_sha_none_when_rev_parse_fails_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        runner = _MockRunner(fail_on="rev-parse", index_stdout="Indexed 3 documents\n")
        result = await _drive(
            bus, ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base"), runner
        )
        assert result.success is True
        assert result.commit_sha is None  # _get_commit_sha swallowed the error
        assert result.entry_count == 3
        assert result.error is None
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Idempotency — identical input yields an identical terminal event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_deterministic_identical_input_over_bus(
    integration_event_bus: Any,
) -> None:
    bus_factory = type(integration_event_bus)
    command = ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base")
    payloads: list[str] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            result = await _drive(
                bus, command, _MockRunner(commit_sha="fixed", index_stdout="42\n")
            )
            payloads.append(result.model_dump_json())
        finally:
            await bus.close()
    assert payloads[0] == payloads[1]
