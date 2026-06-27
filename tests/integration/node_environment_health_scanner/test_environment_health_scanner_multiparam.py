# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_environment_health_scanner
(OMN-13676).

COMPUTE node. The handler's ``handle()`` performs the live ssh/rpk/psql
COLLECTION, but the node's deterministic classification logic lives in pure,
row-injectable probers (their own docstrings: "Called with pre-collected data in
unit tests; SSH collection happens in handler"). We feed synthetic probe rows
directly to those probers — the collector seam is the prober signature itself —
and never monkeypatch subprocess.

Two layers, both real:
  * prober-level param sets (containers/kafka/projections) with negative controls
    that force FAIL/WARN findings;
  * handler-level ``handle(ssh_target=None)`` runs for the kafka + projections
    subsystems are fully deterministic (empty census / no-ssh spec fallback), so
    we assert the node's aggregation wiring end-to-end without any live infra.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_environment_health_scanner.handlers.handler_environment_health_scanner import (
    EnumHealthFindingSeverity,
    EnumSubsystem,
    EnvironmentHealthRequest,
    NodeEnvironmentHealthScanner,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_containers import (
    probe_containers,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_kafka import (
    probe_kafka,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_projections import (
    ModelProjectionSpec,
    probe_projections,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)


def _container(
    name: str, *, running: bool = True, healthy: bool = True, restarts: int = 0
) -> dict[str, object]:
    return {
        "name": name,
        "running": running,
        "healthy": healthy,
        "restart_count": restarts,
        "state": "running" if running else "exited",
    }


# --------------------------------------------------------------------------- #
# probe_containers — multi-param with negative controls                        #
# --------------------------------------------------------------------------- #

_CONTAINER_CASES = [
    pytest.param(
        ["a", "b"],
        [_container("a"), _container("b")],
        EnumReadinessStatus.PASS,
        None,
        id="all-running-healthy",
    ),
    pytest.param(
        ["a", "b"],
        [_container("a")],  # b missing
        EnumReadinessStatus.FAIL,
        EnumHealthFindingSeverity.FAIL,
        id="missing-container",
    ),
    pytest.param(
        ["a"],
        [_container("a", running=False)],
        EnumReadinessStatus.FAIL,
        EnumHealthFindingSeverity.FAIL,
        id="container-not-running",
    ),
    pytest.param(
        ["a"],
        [_container("a", restarts=12)],  # >= fail threshold (10)
        EnumReadinessStatus.FAIL,
        EnumHealthFindingSeverity.FAIL,
        id="crash-looping-container",
    ),
    pytest.param(
        ["a"],
        [_container("a", healthy=False)],
        EnumReadinessStatus.WARN,
        EnumHealthFindingSeverity.WARN,
        id="unhealthy-container",
    ),
    pytest.param(
        ["a"],
        [_container("a", restarts=4)],  # >= warn threshold (3), < fail
        EnumReadinessStatus.WARN,
        EnumHealthFindingSeverity.WARN,
        id="elevated-restart-count",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("expected", "running", "want_status", "want_severity"),
    [(c.values[0], c.values[1], c.values[2], c.values[3]) for c in _CONTAINER_CASES],
    ids=[c.id for c in _CONTAINER_CASES],
)
def test_probe_containers_multiparam(
    expected: list[str],
    running: list[dict[str, object]],
    want_status: EnumReadinessStatus,
    want_severity: EnumHealthFindingSeverity | None,
) -> None:
    result = probe_containers(
        expected_containers=expected, running_containers=running, ssh_target=None
    )
    assert result.subsystem == EnumSubsystem.CONTAINERS
    assert result.status == want_status
    assert result.check_count == len(expected)
    if want_severity is None:
        assert result.findings == []
    else:
        assert any(f.severity == want_severity for f in result.findings)
        assert all(f.subject for f in result.findings)


# --------------------------------------------------------------------------- #
# probe_kafka — multi-param with negative controls                            #
# --------------------------------------------------------------------------- #

_KAFKA_CASES = [
    pytest.param(
        ["onex.evt.omnimarket.foo.v1"],
        ["onex.evt.omnimarket.foo.v1"],
        [
            "evt-omnimarket-foo-consumer"
        ],  # contains derived fragment "evt-omnimarket-foo"
        EnumReadinessStatus.PASS,
        None,
        id="topic-exists-with-consumer",
    ),
    pytest.param(
        ["onex.evt.omnimarket.missing.v1"],
        [],  # topic does not exist
        [],
        EnumReadinessStatus.FAIL,
        EnumHealthFindingSeverity.FAIL,
        id="declared-topic-missing-from-broker",
    ),
    pytest.param(
        ["onex.evt.omnimarket.lonely.v1"],
        ["onex.evt.omnimarket.lonely.v1"],
        [],  # exists but no consumer group
        EnumReadinessStatus.WARN,
        EnumHealthFindingSeverity.WARN,
        id="topic-exists-no-consumer",
    ),
    pytest.param(
        [],  # nothing declared → valid-zero PASS
        [],
        [],
        EnumReadinessStatus.PASS,
        None,
        id="no-declared-topics-valid-zero",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("declared", "existing", "groups", "want_status", "want_severity"),
    [
        (c.values[0], c.values[1], c.values[2], c.values[3], c.values[4])
        for c in _KAFKA_CASES
    ],
    ids=[c.id for c in _KAFKA_CASES],
)
def test_probe_kafka_multiparam(
    declared: list[str],
    existing: list[str],
    groups: list[str],
    want_status: EnumReadinessStatus,
    want_severity: EnumHealthFindingSeverity | None,
) -> None:
    result = probe_kafka(
        declared_topics=declared,
        existing_topics=existing,
        consumer_groups=groups,
        ssh_target=None,
    )
    assert result.subsystem == EnumSubsystem.KAFKA
    assert result.status == want_status
    assert result.check_count == len(declared)
    if want_severity is None:
        assert result.findings == []
    else:
        assert any(f.severity == want_severity for f in result.findings)


# --------------------------------------------------------------------------- #
# probe_projections — multi-param with negative controls                      #
# --------------------------------------------------------------------------- #

_PROJECTION_CASES = [
    pytest.param(
        ModelProjectionSpec(
            table_name="fresh",
            max_freshness_seconds=3600,
            row_count=10,
            last_updated=_NOW - timedelta(minutes=10),
        ),
        EnumReadinessStatus.PASS,
        None,
        id="fresh-populated-table",
    ),
    pytest.param(
        ModelProjectionSpec(table_name="empty", max_freshness_seconds=3600),
        EnumReadinessStatus.WARN,
        EnumHealthFindingSeverity.WARN,
        id="empty-table-no-timestamp",
    ),
    pytest.param(
        ModelProjectionSpec(
            table_name="stale",
            max_freshness_seconds=3600,
            row_count=5,
            last_updated=_NOW - timedelta(hours=1, minutes=30),  # > 1x, < 2x
        ),
        EnumReadinessStatus.WARN,
        EnumHealthFindingSeverity.WARN,
        id="stale-table-warn",
    ),
    pytest.param(
        ModelProjectionSpec(
            table_name="very_stale",
            max_freshness_seconds=3600,
            row_count=5,
            last_updated=_NOW - timedelta(hours=5),  # > 2x threshold → FAIL
        ),
        EnumReadinessStatus.FAIL,
        EnumHealthFindingSeverity.FAIL,
        id="very-stale-table-fail",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("spec", "want_status", "want_severity"),
    [(c.values[0], c.values[1], c.values[2]) for c in _PROJECTION_CASES],
    ids=[c.id for c in _PROJECTION_CASES],
)
def test_probe_projections_multiparam(
    spec: ModelProjectionSpec,
    want_status: EnumReadinessStatus,
    want_severity: EnumHealthFindingSeverity | None,
) -> None:
    result = probe_projections(specs=[spec], now=_NOW)
    assert result.subsystem == EnumSubsystem.PROJECTIONS
    assert result.status == want_status
    assert result.check_count == 1
    if want_severity is None:
        assert result.findings == []
    else:
        assert any(f.severity == want_severity for f in result.findings)
        assert result.findings[0].subject == spec.table_name


# --------------------------------------------------------------------------- #
# Node handler aggregation — deterministic ssh_target=None paths               #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_handler_kafka_empty_census_passes(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """handle() for kafka with no ssh + empty omni_home → zero declared topics → PASS.

    Proves the node assembles a prober result and aggregates overall, fully
    in-memory (no broker). We pin omni_home to an empty tmp dir and strip the
    ambient ssh target so the collectors return empty deterministically — never
    walking the real omni_home tree or shelling out to ssh."""
    monkeypatch.delenv("ONEX_INFRA_SSH_TARGET", raising=False)
    result = NodeEnvironmentHealthScanner().handle(
        EnvironmentHealthRequest(
            subsystems=["kafka"], omni_home=str(tmp_path), ssh_target=None
        )
    )
    assert result.overall == EnumReadinessStatus.PASS
    assert len(result.subsystem_results) == 1
    assert result.subsystem_results[0].subsystem == EnumSubsystem.KAFKA
    assert result.subsystem_results[0].check_count == 0


@pytest.mark.integration
def test_handler_projections_no_ssh_flags_empty_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handle() for projections with no ssh uses the spec fallback
    (row_count=0, last_updated=None for every SLO table) → every table WARNs.

    This is the node's real aggregation path producing a non-trivial WARN
    overall, deterministically and without psql. The ambient ssh target is
    stripped so ``_collect_projection_specs`` takes the no-ssh branch."""
    monkeypatch.delenv("ONEX_INFRA_SSH_TARGET", raising=False)
    result = NodeEnvironmentHealthScanner().handle(
        EnvironmentHealthRequest(subsystems=["projections"], ssh_target=None)
    )
    assert result.overall == EnumReadinessStatus.WARN
    assert len(result.subsystem_results) == 1
    proj = result.subsystem_results[0]
    assert proj.subsystem == EnumSubsystem.PROJECTIONS
    assert proj.check_count >= 1
    assert proj.findings, "expected empty-table WARN findings"
    assert all(f.severity == EnumHealthFindingSeverity.WARN for f in proj.findings)
