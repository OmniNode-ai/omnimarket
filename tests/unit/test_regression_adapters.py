# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's adapters: overlay defaults, gateway runs, change sources, claims, the two state files and the dev build record."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.models.model_github_gateway_io import (
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.change_sources import (
    CompositeChangeSource,
    GitMergeChangeSource,
    LedgerMarkerChangeSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.claims import (
    LedgerClaimHolders,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.gateway_runs import (
    GatewayProbeRunSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.inmemory_adapters import (
    InMemoryDevBuildStore,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.state_files import (
    FileDevBuildStore,
    FileSeenStore,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_dev_build_record import (
    HandlerDevBuildRecord,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumChangeKind,
    EnumRunVerdict,
    ModelChangeMarker,
    ModelMergeSource,
    ModelRebuildCompletedObserved,
    ModelRegressionSettings,
    ModelWatchedCheck,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

NODE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_delegation_check_regression_effect"
)
T = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)


def test_the_overlay_example_is_valid_and_its_defaults_are_the_ticket_thresholds() -> (
    None
):
    settings = ModelRegressionSettings.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    assert settings.max_alarm_latency_seconds == 900
    assert settings.scan_interval_seconds <= settings.max_alarm_latency_seconds
    assert settings.flap_window_seconds == 86400
    assert settings.flap_alarm_count == 2
    assert {c.check_id for c in settings.checks} >= {"C11", "C13", "C14", "C29"}
    assert settings.owner
    assert settings.alarm_lane


def test_the_contract_declares_every_topic_the_handlers_use() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    topics = contract["topics"]
    declared = set(contract["event_bus"]["publish_topics"]) | set(
        contract["event_bus"]["subscribe_topics"]
    )
    assert set(topics.values()) == declared
    assert (
        topics["alarm_delivery_requested"]
        == "onex.cmd.omnimarket.lab-alarm-delivery-requested.v1"
    )


class _FakeGateway:
    def __init__(self, runs: list[ModelWorkflowRunSummary]) -> None:
        self.requests: list[ModelGithubGatewayRequest] = []
        self._runs = runs

    def read(self, request: ModelGithubGatewayRequest) -> ModelWorkflowRunsResult:
        self.requests.append(request)
        return ModelWorkflowRunsResult(
            total_count=len(self._runs),
            truncated=False,
            repo=request.repo,
            runs=self._runs,
        )


def _summary(
    run_id: int, conclusion: str | None, updated: str
) -> ModelWorkflowRunSummary:
    return ModelWorkflowRunSummary(
        id=run_id,
        name="n",
        workflow_id=1,
        head_sha="a" * 40,
        event="schedule",
        status="completed",
        conclusion=conclusion,
        run_number=run_id,
        run_attempt=1,
        created_at=updated,
        updated_at=updated,
        html_url=f"https://example.invalid/{run_id}",
    )


def test_the_gateway_source_reads_by_workflow_file_name_and_keeps_only_passes_and_failures() -> (
    None
):
    gateway = _FakeGateway(
        [
            _summary(3, "failure", "2026-11-23T14:39:17Z"),
            _summary(2, "cancelled", "2026-11-23T13:50:00Z"),
            _summary(1, "success", "2026-11-23T13:39:31Z"),
        ]
    )
    check = ModelWatchedCheck(
        check_id="C14",
        repository="example-org/example-customer",
        workflow=".github/workflows/c14.yml",
        interval_seconds=3600,
    )
    found = GatewayProbeRunSource(30.0, gateway).runs(check, 20)
    assert gateway.requests[0].workflow == "c14.yml"
    assert gateway.requests[0].event == "schedule"
    assert [(r.run_id, r.verdict) for r in found] == [
        ("3", EnumRunVerdict.FAIL),
        ("1", EnumRunVerdict.PASS),
    ]


LEDGER = [
    "2026-11-23T13:46:25Z | RULING | lane=orchestrator | req=r1 | a model leaves the 5090 host",
    "2026-11-23T13:10:00Z | RULING | lane=orchestrator | req=r2 | a model leaves the 5090 host, before the window",
    "2026-11-23T13:20:00Z | CLAIM | lane=fix-c14 | check=C14 | holds the check",
    "2026-11-23T13:30:00Z | CLAIM | lane=fix-c13 | check=C13 | holds the check",
    "2026-11-23T13:40:00Z | TERMINAL | lane=fix-c13 | outcome=done | done",
]


def test_the_ledger_marker_source_names_a_matching_row_inside_the_window_only() -> None:
    source = LedgerMarkerChangeSource(
        InMemoryLedger(LEDGER),
        [
            ModelChangeMarker(
                kind=EnumChangeKind.HOST_CHANGE,
                row_type="RULING",
                contains_all=("5090",),
            )
        ],
    )
    changes = source.between(T - timedelta(minutes=30), T)
    assert [(c.kind, c.ref) for c in changes] == [(EnumChangeKind.HOST_CHANGE, "r1")]


def test_a_check_is_held_by_an_open_claim_and_released_by_the_lanes_terminal() -> None:
    claims = LedgerClaimHolders(InMemoryLedger(LEDGER), T + timedelta(hours=1))
    assert claims.holder("C14") == "fix-c14"
    assert claims.holder("C13") is None
    assert claims.holder("C29") is None


def test_the_git_merge_source_reads_first_parent_history_through_the_clone() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        seen.append(list(argv))
        return "abcdef0123456789\t2026-11-23T13:16:38+00:00\tmerge one\nfeed0000\t2026-11-23T12:00:00+00:00\ttoo early\n"

    source = GitMergeChangeSource(
        Path("/root"),
        [
            ModelMergeSource(
                repository="example-org/example-platform",
                clone="example-platform",
                branch="dev",
            )
        ],
        runner,
    )
    changes = source.between(
        datetime(2026, 11, 23, 13, 0, tzinfo=UTC),
        datetime(2026, 11, 23, 14, 0, tzinfo=UTC),
    )
    assert [c.summary for c in changes] == ["merge one"]
    assert "--first-parent" in seen[0]
    assert "origin/dev" in seen[0]
    assert changes[0].ref == "example-org/example-platform@abcdef012345"


def test_an_unreadable_change_source_is_named_not_dropped() -> None:
    class Broken:
        def between(self, start: datetime, end: datetime) -> tuple[()]:
            raise OSError("git exited 128")

    changes = CompositeChangeSource([Broken()]).between(T - timedelta(hours=1), T)
    assert len(changes) == 1
    assert "unreadable" in changes[0].summary


def test_the_seen_store_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "state" / "seen.json"
    FileSeenStore(path).record("abc")
    assert FileSeenStore(path).seen("abc") is True
    assert FileSeenStore(path).seen("other") is False


def test_a_dev_build_record_keeps_only_the_watched_lane_and_is_read_between_two_runs(
    tmp_path: Path,
) -> None:
    store = FileDevBuildStore(tmp_path / "builds.jsonl")
    handler = HandlerDevBuildRecord(store=store, runtime_lane="dev")
    inside = ModelRebuildCompletedObserved.model_validate(
        {
            "git_sha": "c" * 40,
            "completed_at": T,
            "runtime_lane": "dev",
            "requested_git_ref": "refs/heads/dev",
            "extra_field": "ignored",
        }
    )
    other = ModelRebuildCompletedObserved(
        git_sha="d" * 40, completed_at=T, runtime_lane="stability"
    )
    assert handler.handle(inside).recorded is True
    assert handler.handle(other).recorded is False
    changes = store.between(T - timedelta(minutes=5), T + timedelta(minutes=5))
    assert [c.kind for c in changes] == [EnumChangeKind.DEV_BUILD]
    assert store.between(T + timedelta(minutes=5), T + timedelta(hours=1)) == ()


def test_the_in_memory_dev_build_store_keeps_each_append() -> None:
    store = InMemoryDevBuildStore()
    HandlerDevBuildRecord(store=store, runtime_lane="dev").handle(
        ModelRebuildCompletedObserved(
            git_sha="e" * 40, completed_at=T, runtime_lane="dev"
        )
    )
    assert len(store.builds) == 1


def test_both_alarm_conditions_are_declared_with_a_page_severity_in_the_lab_alarm_table() -> (
    None
):
    from omnimarket.nodes.node_lab_alarm.handlers.alarm_route import (
        load_alarm_severity_table,
    )

    table = load_alarm_severity_table()
    defaults = ModelRegressionSettings.model_fields
    for field in ("regression_condition", "flap_condition"):
        source = table.source(defaults[field].default)
        assert source.severity.value == "page"
        assert source.posts_via == "deliver-effect"


def test_the_bus_publisher_opens_publishes_and_closes_on_one_context() -> None:
    from omnimarket.nodes.node_delegation_check_regression_effect.adapters.bus_publisher import (
        BusRegressionPublisher,
    )
    from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
        ModelDevBuildRecorded,
    )

    payload = ModelDevBuildRecorded(
        git_sha="a" * 40, completed_at=T, runtime_lane="dev", recorded=True
    )
    with BusRegressionPublisher("inmemory", None, None) as publisher:
        publisher.publish(
            "onex.evt.omnimarket.delegation-check-regression-detected.v1",
            "key-1",
            payload,
        )
