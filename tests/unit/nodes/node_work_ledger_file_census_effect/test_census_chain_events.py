# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the ledger file census (OMN-22917, piece 6).

The golden chain is a start then a success under one correlation id. Each error chain ends in its typed
failure and never in a pass: a grown count, an absent repository, an unreadable one, a repository with
no ratchet row, a ratchet file that cannot be read, a name that is not registered.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusRequest,
)
from tests.unit.nodes.node_work_ledger_file_census_effect.support import (
    NOW,
    READER,
    handler,
    repositories,
)

pytestmark = pytest.mark.unit
ZERO = {"alpha": 0, "beta": 0, "outside": 0}


class RaisingSource:
    def present(self, repository: str) -> bool:
        return True

    def candidates(self, repository: str, tokens: Sequence[str]) -> tuple[str, ...]:
        raise OSError("disk read failed")

    def read(self, repository: str, path: str) -> str | None:
        raise OSError("disk read failed")


def phases(sink: InMemoryEmissionSink) -> list[tuple[str, str | None]]:
    return [(e.phase, e.failure_code) for e in sink.events]


def test_golden_chain_ends_in_success_under_one_correlation_id() -> None:
    census, sink = handler(repositories(), ZERO)
    census.handle(ModelCensusRequest(correlation_id="golden-1"))
    assert phases(sink) == [("start", None), ("success", None)]
    assert {e.correlation_id for e in sink.events} == {"golden-1"}
    assert {(e.node, e.piece, e.backend) for e in sink.events} == {
        ("node_work_ledger_file_census_effect", 6, "in_memory")
    }
    assert sink.events[-1].counters == {"files": 0, "repositories": 3}
    assert sink.events[0].emitted_at == NOW


def test_error_chain_grown_count_ends_in_ratchet_grew() -> None:
    census, sink = handler(repositories(beta={"x.py": READER}), ZERO)
    result = census.handle(ModelCensusRequest(correlation_id="grew"))
    assert not result.passed
    assert phases(sink) == [("start", None), ("failure", "ratchet-grew")]


def test_error_chain_absent_repository() -> None:
    repos = repositories()
    del repos["beta"]
    census, sink = handler(repos, ZERO)
    result = census.handle(ModelCensusRequest(correlation_id="absent"))
    assert result.failure_code == "repo-absent"
    assert phases(sink)[-1] == ("failure", "repo-absent")


def test_error_chain_unreadable_repository_is_never_a_pass() -> None:
    census, sink = handler(repositories(), ZERO, source=RaisingSource())
    result = census.handle(ModelCensusRequest(correlation_id="unreadable"))
    assert not result.passed
    assert phases(sink)[-1] == ("failure", "repo-unreadable")


def test_error_chain_repository_without_a_ratchet_row() -> None:
    census, sink = handler(repositories(), {"alpha": 0, "outside": 0})
    result = census.handle(ModelCensusRequest(correlation_id="unratcheted"))
    assert result.failure_code == "ratchet-missing-row"
    assert phases(sink)[-1] == ("failure", "ratchet-missing-row")


def test_error_chain_unreadable_ratchet_file() -> None:
    census, sink = handler(repositories(), None)
    result = census.handle(ModelCensusRequest(correlation_id="no-ratchet"))
    assert result.failure_code == "ratchet-unreadable"
    assert phases(sink) == [("start", None), ("failure", "ratchet-unreadable")]


def test_error_chain_unregistered_repository_name() -> None:
    census, sink = handler(repositories(), ZERO)
    result = census.handle(
        ModelCensusRequest(correlation_id="unregistered", repositories=("gamma",))
    )
    assert result.failure_code == "repository-not-registered"
    assert phases(sink)[-1] == ("failure", "repository-not-registered")
