# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_build_delegation_checks: once per build, a record per check, suspects on a new red, and the owner the alarm needs."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.models.delegation_check_regression import ModelRegressionEvent
from omnimarket.models.lab_alarm_delivery import EnumDeliveryCondition
from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    DELEGATION_CHECKS,
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_build_delegation_checks.adapters.command_check_executor import (
    CommandCheckExecutor,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.file_build_store import (
    FileBuildStore,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.git_merge_source import (
    parse_log,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.inmemory_adapters import (
    InMemoryBuildStore,
    InMemoryMergeSource,
    RecordedResultsExecutor,
    RecordingSink,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumBuildChecksStatus,
    EnumCheckReading,
    ModelCheckOutcome,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_build_delegation_checks.overlay import load_overlay

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_build_delegation_checks"
)
B1, B2, B3 = "1" * 40, "2" * 40, "3" * 40
T0 = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)
PASS = ModelCheckOutcome(reading=EnumCheckReading.PASS, run_id="r-pass")
FAIL = ModelCheckOutcome(reading=EnumCheckReading.FAIL, run_id="r-fail")
GONE = ModelCheckOutcome(reading=EnumCheckReading.UNAVAILABLE, detail="host down")


def _merge(sha: str, pr: int | None, minutes: int) -> ModelAttributionCandidate:
    return ModelAttributionCandidate(
        commit_sha=sha, pr_number=pr, merged_at=T0 + timedelta(minutes=minutes)
    )


def _settings(**changes: object) -> ModelBuildDelegationChecksOverlay:
    base: dict[str, object] = {"checks": ("C14", "C29"), "owner": "delegation-owner"}
    return ModelBuildDelegationChecksOverlay(**(base | changes))


def _event(
    sha: str, lane: str = "dev", status: Literal["success", "failed"] = "success"
) -> ModelRebuildCompletedObserved:
    return ModelRebuildCompletedObserved(git_sha=sha, runtime_lane=lane, status=status)


class _Rig:
    def __init__(
        self,
        recorded: dict[str, dict[str, ModelCheckOutcome]],
        merges: dict[tuple[str, str], tuple[ModelAttributionCandidate, ...]]
        | None = None,
        settings: ModelBuildDelegationChecksOverlay | None = None,
    ) -> None:
        self.runner = RecordedResultsExecutor(recorded)
        self.store = InMemoryBuildStore()
        self.sink = RecordingSink()
        self.handler = HandlerBuildDelegationChecks(
            settings=settings or _settings(),
            runner=self.runner,
            store=self.store,
            merges=InMemoryMergeSource(merges or {}),
            sink=self.sink,
            clock=lambda: T0 + timedelta(minutes=90),
        )


def test_once_per_build_a_replayed_rebuild_runs_nothing() -> None:
    rig = _Rig({B1: {"C14": PASS, "C29": PASS}})
    first = rig.handler.handle(_event(B1))
    calls_after_first = list(rig.runner.calls)
    second = rig.handler.handle(_event(B1))

    assert first.status is EnumBuildChecksStatus.RAN
    assert calls_after_first == [("C14", B1), ("C29", B1)]
    assert second.status is EnumBuildChecksStatus.ALREADY_RUN
    assert rig.runner.calls == calls_after_first  # the replay ran no probe
    assert len(rig.store.builds) == 1
    assert len(rig.sink.results) == 1  # and published nothing


def test_only_a_successful_dev_rebuild_runs_checks() -> None:
    rig = _Rig({B1: {"C14": PASS}})
    assert (
        rig.handler.handle(_event(B1, lane="stability")).status
        is EnumBuildChecksStatus.SKIPPED
    )
    assert (
        rig.handler.handle(_event(B1, status="failed")).status
        is EnumBuildChecksStatus.SKIPPED
    )
    assert rig.runner.calls == []
    assert rig.store.builds == []


def test_records_per_check_carry_the_build_id_and_the_merge_list() -> None:
    added = (_merge("a" * 40, 6001, 1), _merge("b" * 40, None, 2))
    rig = _Rig(
        {B1: {"C14": PASS, "C29": PASS}, B2: {"C14": PASS, "C29": GONE}},
        {(B1, B2): added},
    )
    rig.handler.handle(_event(B1))
    result = rig.handler.handle(_event(B2))

    assert result.previous_build_id == B1
    assert {r.check_id for r in result.records} == {"C14", "C29"}
    for record in result.records:
        assert record.build_id == B2
        assert record.previous_build_id == B1
        assert [m.commit_sha for m in record.merges] == ["a" * 40, "b" * 40]
        assert [m.pr_number for m in record.merges] == [6001, None]
    readings = {r.check_id: r.reading for r in result.records}
    assert readings == {
        "C14": EnumCheckReading.PASS,
        "C29": EnumCheckReading.UNAVAILABLE,
    }
    kept = rig.store.builds[-1]
    assert kept.complete
    assert kept.build_id == B2
    assert len(kept.checks) == 2


def test_suspects_name_the_merges_and_a_repeat_red_raises_nothing() -> None:
    added = (_merge("a" * 40, 6001, 1), _merge("b" * 40, 6002, 2))
    rig = _Rig(
        {
            B1: {"C14": PASS, "C29": FAIL},
            B2: {"C14": FAIL, "C29": FAIL},
            B3: {"C14": FAIL, "C29": FAIL},
        },
        {(B1, B2): added},
    )
    rig.handler.handle(_event(B1))
    on_red = rig.handler.handle(_event(B2))

    assert [e.check_id for e in on_red.regressions] == [
        "C14"
    ]  # C29 was already red: no event
    event = on_red.regressions[0]
    assert event.source.value == "dev_build"
    assert event.build_id == B2
    assert event.previous_build_id == B1
    assert [s.pr_number for s in event.suspects] == [6001, 6002]
    assert event.owner == "delegation-owner"
    assert event.event_id == f"C14:{B2}"
    assert len(on_red.alarms) == 1
    alarm = on_red.alarms[0]
    assert alarm.outcome is EnumDeliveryCondition.ALARM
    assert "#6001" in alarm.alarms[0].detail
    assert "#6002" in alarm.alarms[0].detail
    assert "delegation-owner" in alarm.alarms[0].detail
    assert rig.sink.regressions == [event]

    again = rig.handler.handle(_event(B3))  # still red on the next build: nothing new
    assert again.regressions == ()
    assert again.alarms == ()
    assert len(rig.sink.regressions) == 1
    assert len(rig.sink.alarms) == 1


def test_a_check_that_could_not_run_is_unavailable_never_a_regression() -> None:
    rig = _Rig({B1: {"C14": PASS, "C29": PASS}, B2: {"C14": GONE}, B3: {"C14": FAIL}})
    rig.handler.handle(_event(B1))
    down = rig.handler.handle(_event(B2))
    assert down.regressions == ()
    assert down.alarms == ()
    assert {r.reading for r in down.records} == {EnumCheckReading.UNAVAILABLE}
    # The last reading that counts is still the pass on B1, so the red on B3 names B1 as the last green.
    red = rig.handler.handle(_event(B3))
    assert [e.check_id for e in red.regressions] == ["C14"]
    assert red.regressions[0].previous_build_id == B1


def test_owner_required_refuses_an_unowned_alarm() -> None:
    for owner in (None, "", "   "):
        rig = _Rig(
            {B1: {"C14": PASS}, B2: {"C14": FAIL}},
            settings=_settings(owner=owner, checks=("C14",)),
        )
        rig.handler.handle(_event(B1))
        result = rig.handler.handle(_event(B2))

        assert result.regressions == ()
        assert result.alarms == ()
        assert [r.check_id for r in result.refused] == ["C14"]
        assert "owner-missing" in result.refused[0].reason
        assert rig.sink.regressions == []
        assert rig.sink.alarms == []
        assert [r.reading for r in result.records] == [
            EnumCheckReading.FAIL
        ]  # the reading is still recorded


def test_the_overlay_example_holds_the_tested_defaults() -> None:
    example = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    loaded = ModelBuildDelegationChecksOverlay.model_validate(example)
    assert loaded.checks == DELEGATION_CHECKS
    assert len(loaded.checks) == 10
    assert loaded.owner == "delegation-owner"
    assert loaded.max_alarm_latency_seconds == 900
    assert loaded.check_timeout_seconds == 1800
    assert loaded.alarm_condition == "delegation_check_regression"
    # The model's own defaults agree with the example, except the owner, which has none.
    bare = ModelBuildDelegationChecksOverlay()
    assert bare.owner is None
    assert bare.model_copy(update={"owner": loaded.owner}) == loaded
    # The settings are read from the overlay the store serves.
    pinned = load_overlay(
        {"ONEX_BUILD_DELEGATION_CHECKS_OVERLAY": str(NODE_DIR / "overlay.example.yaml")}
    )
    assert pinned == loaded


class _EventTopicRefused(RecordingSink):
    def publish_regression(self, event: ModelRegressionEvent) -> None:
        raise OSError("topic not authorized")


def test_a_refused_event_publish_does_not_silence_the_alarm() -> None:
    rig = _Rig({B1: {"C14": PASS}, B2: {"C14": FAIL}})
    rig.sink = _EventTopicRefused()
    rig.handler = HandlerBuildDelegationChecks(
        settings=_settings(checks=("C14",)),
        runner=rig.runner,
        store=rig.store,
        merges=InMemoryMergeSource({}),
        sink=rig.sink,
    )
    rig.handler.handle(_event(B1))
    result = rig.handler.handle(_event(B2))
    assert len(rig.sink.alarms) == 1
    assert [r.reason for r in result.refused] == ["publish-failed (event): OSError"]


def test_a_store_serving_no_overlay_loads_the_defaults_with_no_owner(
    tmp_path: Path,
) -> None:
    loaded = load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert loaded == ModelBuildDelegationChecksOverlay()
    assert loaded.owner is None
    assert len(loaded.checks) == 10


def test_no_prod_target_and_no_pinned_model_in_the_node() -> None:
    prod = re.compile(r"(?<![a-z])prod(?![a-z])")
    pinned = ("qwen", "inkling", "gpt-", "claude-", "gemini")
    scanned = 0
    for path in NODE_DIR.rglob("*"):
        if path.suffix not in {".py", ".yaml"}:
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8").lower()
        assert not prod.search(text), path.name
        assert not [name for name in pinned if name in text], path.name
    assert scanned >= 15  # positive control: the scan read the node's files
    assert prod.search(
        "the prod target"
    )  # positive control: the pattern matches a prod target
    assert not prod.search("externally_produced_topics")


def test_command_runner_reads_exit_codes_and_a_missing_route_is_unavailable() -> None:
    runner = CommandCheckExecutor(
        {
            "C11": ("true",),
            "C12": ("false",),
            "C13": ("sh", "-c", "exit 75"),
            "C14": ("no-such-probe-binary-xyz",),
            "C15": ("sleep", "5"),
        }
    )
    assert runner.run("C11", B1, 5).reading is EnumCheckReading.PASS
    assert runner.run("C12", B1, 5).reading is EnumCheckReading.FAIL
    assert (
        runner.run("C13", B1, 5).reading is EnumCheckReading.UNAVAILABLE
    )  # exit 75: could not measure
    assert (
        runner.run("C14", B1, 5).reading is EnumCheckReading.UNAVAILABLE
    )  # could not start
    assert (
        runner.run("C15", B1, 0.2).reading is EnumCheckReading.UNAVAILABLE
    )  # timed out
    assert (
        runner.run("C16", B1, 5).reading is EnumCheckReading.UNAVAILABLE
    )  # no route declared


def test_file_store_keeps_builds_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "builds.json"
    rig = _Rig({B1: {"C14": PASS, "C29": PASS}})
    rig.handler = HandlerBuildDelegationChecks(
        settings=_settings(),
        runner=rig.runner,
        store=FileBuildStore(path, 50),
        merges=InMemoryMergeSource({}),
        sink=rig.sink,
    )
    rig.handler.handle(_event(B1))
    reopened = FileBuildStore(path, 50)
    previous = reopened.previous_build("dev", B2)
    assert previous is not None
    assert reopened.begin(previous.model_copy()) is False
    last = reopened.last_reading("C14", B2)
    assert last is not None
    assert last.build_id == B1
    assert last.reading is EnumCheckReading.PASS


def test_git_log_lines_become_candidates_oldest_first_with_the_pr_number() -> None:
    text = (
        f"{B2}|2026-11-23T14:25:56Z|add a thing (#6635)\n"
        f"{B1}|2026-11-23T07:16:38-06:00|chore: release without a pr number\n"
    )
    found = parse_log(text)
    assert [c.commit_sha for c in found] == [B1, B2]
    assert [c.pr_number for c in found] == [None, 6635]


def test_the_event_names_the_shared_model() -> None:
    from omnimarket.models.delegation_check_regression import (
        ModelRegressionEvent as Shared,
    )

    assert ModelRegressionEvent is Shared
