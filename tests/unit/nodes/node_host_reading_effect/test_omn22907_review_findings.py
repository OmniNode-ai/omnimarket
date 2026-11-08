# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings of OMN-22907: one host's exception aborted the others, and an empty host set exited 0."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.model_join_j2 import (
    EnumJoinJ2Outcome,
    ModelJoinJ2HostResult,
    ModelJoinJ2Settings,
)
from omnimarket.nodes.node_host_reading_effect.handlers.join_j2 import JoinJ2
from omnimarket.nodes.node_host_reading_effect.runtime_host_reading import (
    all_hosts_golden,
)

pytestmark = pytest.mark.unit

SETTINGS = ModelJoinJ2Settings(
    canary_piece=2,
    seam_piece=3,
    canary_node="node_host_reading_effect",
    seam_node="node_lab_placement_compute",
    repo="OmniNode-ai/omnimarket",
    commit_sha="4c3985a451847571e2cebd06a6edf1940069946e",
    argv=("python3", "-c", "print('J2-CANARY {nonce}')"),
    result_marker="J2-CANARY",
    kind="other",
    lane="host-canary",
    command_timeout_seconds=1,
    result_wait_slack_seconds=1,
    advert_max_age_seconds=60,
    need_tools=("git",),
)


class _FirstReadRaises:
    """A caller whose first advertisement read raises and whose later reads find no advertisement."""

    def __init__(self) -> None:
        self.calls = 0

    def advertisements(self) -> list[Any]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("bus publish refused")
        return []

    def place(self, **_: Any) -> Any:
        raise AssertionError("never reached")

    async def dispatch(self, request: Any) -> Any:
        raise AssertionError("never reached")


def _join(caller: Any) -> JoinJ2:
    return JoinJ2(
        settings=SETTINGS,
        caller=caller,
        sink=InMemoryEmissionSink(),
        backend="in_memory",
        clock=lambda: datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
    )


def test_one_hosts_exception_does_not_abort_the_other_hosts_results() -> None:
    results = asyncio.run(_join(_FirstReadRaises()).run_hosts(("h-a", "h-b")))
    assert [r.host for r in results] == ["h-a", "h-b"]
    assert results[0].outcome is EnumJoinJ2Outcome.NO_RESULT
    assert "bus publish refused" in results[0].detail
    assert results[1].outcome is EnumJoinJ2Outcome.NO_ADVERTISEMENT
    assert not any(r.ok for r in results)


def _golden(host: str) -> ModelJoinJ2HostResult:
    return ModelJoinJ2HostResult(
        host=host, correlation_id="c", outcome=EnumJoinJ2Outcome.GOLDEN
    )


def test_a_run_over_no_hosts_checked_nothing_and_is_not_ok() -> None:
    assert all_hosts_golden(()) is False


def test_a_run_where_every_host_is_golden_is_ok() -> None:
    assert all_hosts_golden((_golden("h-a"), _golden("h-b"))) is True


def test_a_run_with_one_failed_host_is_not_ok() -> None:
    failed = ModelJoinJ2HostResult(
        host="h-b", correlation_id="c", outcome=EnumJoinJ2Outcome.NO_ADVERTISEMENT
    )
    assert all_hosts_golden((_golden("h-a"), failed)) is False
