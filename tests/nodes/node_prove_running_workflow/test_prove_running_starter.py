# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The starter: one workflow start per NOT RUNNING line of the integration pass's typed batch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_prove_running_workflow.handlers import (
    HandlerProveRunningStarter,
)
from omnimarket.nodes.node_prove_running_workflow.models import (
    ModelProveRunningStartRequest,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _reading(
    repo: str,
    number: int,
    state: EnumIntegrationState,
    hours: int,
    target: str = "dev runtime",
) -> ModelIntegrationReading:
    cells: dict[str, Any] = {"gap_class": EnumIntegrationGapClass.NONE}
    if state is EnumIntegrationState.NOT_RUNNING:
        cells = {
            "gap_class": EnumIntegrationGapClass.RUNTIME_BEHIND,
            "gap": "not in the build",
            "step": "roll dev",
        }
    elif state is EnumIntegrationState.RUNNING:
        cells.update(running_path="node_x", running_line="dispatched at 12:00Z")
    return ModelIntegrationReading(
        repository=repo,
        number=number,
        title=f"change {number}",
        merged_at=T0 - timedelta(hours=hours),
        target=target,
        state=state,
        read_at=T0,
        origin="integration-pass-test",
        **cells,
    )


BATCH = ModelIntegrationReadingBatch(
    as_of=T0,
    origin="integration-pass-test",
    readings=(
        _reading("omnimarket", 6001, EnumIntegrationState.RUNNING, 9),
        _reading("omnimarket", 6002, EnumIntegrationState.NOT_RUNNING, 2),
        _reading(
            "omnibase_infra",
            5990,
            EnumIntegrationState.NOT_RUNNING,
            7,
            target="h051 unit",
        ),
        _reading("overlays", 530, EnumIntegrationState.CI_ONLY, 8),
        _reading("omnimarket", 6003, EnumIntegrationState.NOT_RUNNING, 5),
        _reading("omnimarket", 6004, EnumIntegrationState.NOT_RUNNING, 1),
    ),
)


class _Resolver:
    def __init__(self, missing: frozenset[str] = frozenset()) -> None:
        self.asked: list[str] = []
        self._missing = missing

    def merge_sha(self, repository: str, number: int) -> str | None:
        key = f"{repository}#{number}"
        self.asked.append(key)
        return None if key in self._missing else f"{number:07d}abcdef"


def test_not_running_lines_one_start_per_line_oldest_first() -> None:
    resolver = _Resolver()
    out = HandlerProveRunningStarter(resolver).handle(
        ModelProveRunningStartRequest(batch=BATCH)
    )
    assert [c.workflow_id for c in out.changes] == [
        "omnibase_infra#5990@0005990abcdef",
        "omnimarket#6003@0006003abcdef",
        "omnimarket#6002@0006002abcdef",
        "omnimarket#6004@0006004abcdef",
    ]
    assert [c.target for c in out.changes] == [
        "h051-unit",
        "dev-runtime",
        "dev-runtime",
        "dev-runtime",
    ]
    assert resolver.asked == [
        "omnibase_infra#5990",
        "omnimarket#6003",
        "omnimarket#6002",
        "omnimarket#6004",
    ]
    assert out.skipped == ()
    assert out.unresolved == ()
    assert out.lines_read == 4


def test_not_running_lines_skip_running_and_settled_lines() -> None:
    out = HandlerProveRunningStarter(_Resolver()).handle(
        ModelProveRunningStartRequest(batch=BATCH)
    )
    keys = {c.key for c in out.changes}
    assert keys == {
        "omnimarket#6002",
        "omnimarket#6003",
        "omnimarket#6004",
        "omnibase_infra#5990",
    }


def test_not_running_lines_unread_sha_is_skipped_with_reason() -> None:
    resolver = _Resolver(missing=frozenset({"omnibase_infra#5990"}))
    out = HandlerProveRunningStarter(resolver).handle(
        ModelProveRunningStartRequest(batch=BATCH)
    )
    assert [c.key for c in out.changes] == [
        "omnimarket#6003",
        "omnimarket#6002",
        "omnimarket#6004",
    ]
    assert out.skipped == ("omnibase_infra#5990: merge sha unread",)
    (unread,) = out.unresolved
    assert (unread.key, unread.target, unread.next_step) == (
        "omnibase_infra#5990",
        "h051-unit",
        "roll dev",
    )
    assert out.lines_read == len(out.changes) + len(out.unresolved) == 4


async def test_not_running_lines_start_one_workflow_each_and_never_twice() -> None:
    pytest.importorskip("temporalio")
    from temporalio.testing import WorkflowEnvironment

    from omnimarket.nodes.node_prove_running_workflow.runtime_prove_running import (
        contract_config,
        start_changes,
    )

    out = HandlerProveRunningStarter(_Resolver()).handle(
        ModelProveRunningStartRequest(batch=BATCH)
    )
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        # No worker on this queue: the starts are recorded and stay open, so a second pass joins them.
        first = await start_changes(
            env.client, out.changes, contract_config(), "starter-test"
        )
        second = await start_changes(
            env.client, out.changes, contract_config(), "starter-test"
        )
        assert [s["workflow_id"] for s in first] == [c.workflow_id for c in out.changes]
        assert [s["run_id"] for s in second] == [s["run_id"] for s in first]
        for c in out.changes:
            described = await env.client.get_workflow_handle(c.workflow_id).describe()
            assert described.id == c.workflow_id
        for c in out.changes:
            await env.client.get_workflow_handle(c.workflow_id).terminate("test end")
        third = await start_changes(
            env.client, out.changes, contract_config(), "starter-test"
        )
        assert {s["start"] for s in third} == {"refused-already-proven"}
    finally:
        await env.shutdown()


SHA = "270e510aaf9da7fb52ba45df4ede46af2444b83c"


def test_parse_merge_sha_reads_the_verbs_json_line() -> None:
    from omnimarket.nodes.node_prove_running_workflow.handlers.handler_prove_running_merge_sha import (
        parse_merge_sha,
    )

    out = (
        '{"verb":"pr-merge-commit","outcome":"completed","result":{"repo":"OmniNode-ai/omnimarket",'
        f'"number":5752,"merged":true,"merge_commit_sha":"{SHA}"}}}}\n'
    )
    assert parse_merge_sha(out) == SHA


def test_parse_merge_sha_reads_a_bare_sha_and_refuses_the_rest() -> None:
    from omnimarket.nodes.node_prove_running_workflow.handlers.handler_prove_running_merge_sha import (
        parse_merge_sha,
    )

    assert parse_merge_sha(f"merge commit {SHA}\n") == SHA
    assert (
        parse_merge_sha('{"result":{"merged":false,"merge_commit_sha":null}}') is None
    )
    assert parse_merge_sha('{"result":{"merge_commit_sha":"abc"}}') is None
    assert parse_merge_sha("") is None
