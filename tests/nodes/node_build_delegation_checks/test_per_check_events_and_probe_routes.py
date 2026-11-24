# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rebuild-completed event runs omnimarket's own probe nodes and puts one result per check on the bus (OMN-26043).

The node's default routes name the delegation check probe node's tick, never a cloud workflow dispatch; a check with no node of
ours reads unavailable with that reason and never green. Every one of the ten checks publishes its own record on the per-check
topic, beside the per-build record.
"""

from __future__ import annotations

import importlib.util
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    DELEGATION_CHECKS,
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.nodes.node_build_delegation_checks.adapters import (
    command_check_executor,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.command_check_executor import (
    CommandCheckExecutor,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.inmemory_adapters import (
    InMemoryBuildStore,
    InMemoryMergeSource,
    RecordingSink,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumBuildChecksStatus,
    EnumCheckReading,
    ModelRebuildCompletedObserved,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_build_delegation_checks"
)
PROBE_MODULE = "omnimarket.nodes.node_delegation_check_probe_effect.runtime_delegation_check_probe_tick"
OUR_NODE_CHECKS = ("C13", "C14", "C17")
CHECK_TOPIC = "onex.evt.omnimarket.build-delegation-check-recorded.v1"
BUILD = "a" * 40


class _Capture:
    """Stands in for the process spawn: records each probe command and answers exit 0."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **_: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")


def _handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[HandlerBuildDelegationChecks, RecordingSink, _Capture]:
    capture = _Capture()
    monkeypatch.setattr(command_check_executor.subprocess, "run", capture)
    settings = ModelBuildDelegationChecksOverlay(owner="delegation-owner")
    sink = RecordingSink()
    handler = HandlerBuildDelegationChecks(
        settings=settings,
        runner=CommandCheckExecutor(
            settings.check_routes,
            registry_root=tmp_path,
            state_root=tmp_path / "state",
        ),
        store=InMemoryBuildStore(),
        merges=InMemoryMergeSource({}),
        sink=sink,
        clock=lambda: datetime(2026, 11, 24, 7, 0, tzinfo=UTC),
    )
    return handler, sink, capture


def test_the_default_routes_run_the_probe_node_for_each_check_it_owns() -> None:
    routes = ModelBuildDelegationChecksOverlay().check_routes
    assert set(routes) == set(OUR_NODE_CHECKS)
    for check in OUR_NODE_CHECKS:
        argv = routes[check]
        assert argv[:3] == ("{python}", "-m", PROBE_MODULE)
        assert argv[argv.index("--check") + 1] == check
        text = " ".join(argv).lower()
        for cloud in ("workflow", "gh ", "dispatch", "curl"):
            assert cloud not in text, (check, cloud)
    assert importlib.util.find_spec(PROBE_MODULE) is not None


def test_a_rebuild_completed_runs_the_probe_node_and_never_a_workflow_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, _, capture = _handler(tmp_path, monkeypatch)
    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=BUILD, runtime_lane="dev")
    )
    assert result.status is EnumBuildChecksStatus.RAN
    ran = {c[c.index("--check") + 1]: c for c in capture.commands}
    assert set(ran) == set(OUR_NODE_CHECKS)
    for command in capture.commands:
        assert command[1:3] == ["-m", PROBE_MODULE]
        assert command[0] != "{python}"
        assert str(tmp_path) in command  # --omni-home
    readings = {r.check_id: r.reading for r in result.records}
    for check in OUR_NODE_CHECKS:
        assert readings[check] is EnumCheckReading.PASS
    # A check with no node of ours is unavailable with the reason, never green and never a cloud dispatch.
    for check in set(DELEGATION_CHECKS) - set(OUR_NODE_CHECKS):
        assert readings[check] is EnumCheckReading.UNAVAILABLE
    detail = {r.check_id: r.detail for r in result.records}
    assert detail["C29"] == "no probe route is declared for this check"


def test_the_record_names_the_probe_node_that_ran_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, _, _ = _handler(tmp_path, monkeypatch)
    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=BUILD, runtime_lane="dev")
    )
    probes = {r.check_id: r.probe for r in result.records}
    assert {probes[c] for c in OUR_NODE_CHECKS} == {PROBE_MODULE}
    assert {probes[c] for c in set(DELEGATION_CHECKS) - set(OUR_NODE_CHECKS)} == {""}


def test_each_of_the_ten_checks_publishes_one_record_on_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, sink, _ = _handler(tmp_path, monkeypatch)
    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=BUILD, runtime_lane="dev")
    )
    assert [r.check_id for r in sink.check_records] == list(DELEGATION_CHECKS)
    assert len(sink.check_records) == 10
    assert all(r.build_id == BUILD for r in sink.check_records)
    assert tuple(sink.check_records) == result.records
    assert len(sink.results) == 1  # the per-build record is still one


def test_a_refused_per_check_publish_is_named_and_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Refuses(RecordingSink):
        def publish_check_record(self, record: Any) -> None:
            if record.check_id == "C13":
                raise OSError("topic not authorized")
            super().publish_check_record(record)

    handler, _, _ = _handler(tmp_path, monkeypatch)
    sink = _Refuses()
    handler = HandlerBuildDelegationChecks(
        settings=ModelBuildDelegationChecksOverlay(owner="delegation-owner"),
        runner=CommandCheckExecutor({}, registry_root=tmp_path),
        store=InMemoryBuildStore(),
        merges=InMemoryMergeSource({}),
        sink=sink,
    )
    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=BUILD, runtime_lane="dev")
    )
    assert len(sink.check_records) == 9
    assert [r.reason for r in result.refused] == ["publish-failed (check): OSError"]
    assert [r.check_id for r in result.refused] == ["C13"]


def test_the_contract_declares_the_per_check_topic_publishes_and_grants_it() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["topics"]["check_recorded"] == CHECK_TOPIC
    assert CHECK_TOPIC in contract["event_bus"]["publish_topics"]
    grants = {
        (g["name"], g["operation"])
        for entry in contract["broker_acl_grants"]
        for g in entry["grants"]
    }
    assert (CHECK_TOPIC, "WRITE") in grants
    assert (CHECK_TOPIC, "DESCRIBE") in grants
