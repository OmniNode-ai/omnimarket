# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A claims read that exits 69 once is asked again, so a lane opens its own pull request (OMN-24562).

Lab lanes on h051 ended ``needs-pr-open`` on 2026-11-25 because the guard's ledger reader answered exit 69 (bus session failed)
and the guard stayed ``unchecked``. The guard asks a failed read once more (OMN-26387); these tests pin that a first read that
exits 69 and a second that answers is judged on the answer, and that two reads that exit 69 still leave the guard unchecked.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from omnimarket.handlers.scm import scm_cli
from omnimarket.handlers.scm.scm_duplicate_guard import EXIT_UNCHECKED
from tests.unit.handlers.scm.test_scm_duplicate_guard import World, invoke
from tests.unit.handlers.scm.test_scm_duplicate_guard_reader_timeout import (
    HOSTS,
    Broker,
    other_lane_claim_rows,
    sent,  # noqa: F401  (fixture)
    use_broker,
    world,  # noqa: F401  (fixture)
)

pytestmark = pytest.mark.unit


class FlakyBroker(Broker):
    """Answers the first ``exit69_reads`` reads with the host's recorded exit 69, then answers normally."""

    def __init__(self, *args: Any, exit69_reads: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.exit69_reads = exit69_reads

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if len(self.argvs) < self.exit69_reads:
            self.argvs.append(argv)
            return subprocess.CompletedProcess(argv, 69, "", self.recorded["stderr"])
        return super().__call__(argv, **kwargs)


@pytest.mark.parametrize("host", HOSTS)
def test_a_first_read_that_exits_69_is_retried_and_the_answer_is_judged(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    broker = use_broker(
        monkeypatch, FlakyBroker(other_lane_claim_rows(), host, exit69_reads=1)
    )

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, (
        f"{host}: {result.stderr}; reads {broker.argvs}"
    )
    assert json.loads(result.stdout)["adopt_lane"] == "cause-core-0010"
    assert broker.argvs[1] == broker.argvs[0]


@pytest.mark.parametrize("host", HOSTS)
def test_two_reads_that_exit_69_leave_the_guard_unchecked_and_open_nothing(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    sent: list[object],  # noqa: F811
    host: str,
) -> None:
    use_broker(monkeypatch, FlakyBroker(other_lane_claim_rows(), host, exit69_reads=2))

    result = invoke(world.check_args())

    assert result.exit_code == EXIT_UNCHECKED, result.output
    assert sent == []
