# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topic reaper has a caller: a runtime-tick node that reads the broker and calls the reaper, dry run only.

Built from the real contracts. ``node_topic_reaper_compute`` declares it is called in process and subscribes to
nothing, so without a caller in ``src`` it never runs. These tests fail on a tree with no such caller.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_topic_reaper_compute.models.model_topic_reaper import (
    REAPER_POLICY_MISSING,
    ModelReaperObservation,
    ModelTopicReaperRequest,
    ModelTopicReaperResult,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.handlers.handler_trigger_declaration_check import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models import (
    ModelTriggerCheckRequest,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = REPO / "src/omnimarket/nodes"
REAPER = "node_topic_reaper_compute"
CALLER = "node_topic_reaper_scheduled_effect"
CALLER_DIR = NODES / CALLER
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
LOGGER = "omnimarket.nodes.node_topic_reaper_scheduled_effect.handlers.handler_topic_reaper_scheduled"


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def _overlay_root(
    tmp_path: Path, *, policy: bool, enforce: bool = False
) -> dict[str, str]:
    if policy:
        (tmp_path / REAPER).mkdir()
        (tmp_path / REAPER / "overlay.yaml").write_text(
            yaml.safe_dump(
                {
                    "lanes": {
                        "dev": {
                            "quiet_days_before_flag": 1,
                            "flag_to_delete_days": 1,
                            "quiet_observations_required": 1,
                            "enforce": enforce,
                            "protected_prefixes": ["_"],
                            "baseline_topics": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _quiet_topic(name: str) -> ModelReaperObservation:
    """A topic with a full reading that shows nothing produced or consumed since long ago."""
    return ModelReaperObservation(
        topic=name,
        partitions=3,
        topic_id="id-" + name,
        end_offset_total=0,
        group_members=0,
        committed_groups=(),
        created_at=T0 - timedelta(days=60),
    )


class _Evidence:
    def __init__(self, observations: tuple[ModelReaperObservation, ...]) -> None:
        self.observations = observations
        self.reads = 0

    def read(self) -> Any:
        from omnimarket.nodes.node_topic_reaper_scheduled_effect.protocols import (
            ModelReaperEvidence,
        )

        self.reads += 1
        return ModelReaperEvidence(
            observations=self.observations, boot_set_topics=("declared.topic.v1",)
        )


class _SpyReaper:
    """Stands in for HandlerTopicReaper and records every request it is handed."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[ModelTopicReaperRequest] = []

    def handle(self, request: ModelTopicReaperRequest) -> ModelTopicReaperResult:
        self.requests.append(request)
        return self.inner.handle(request)  # type: ignore[no-any-return]


def _handler(environ: dict[str, str], evidence: _Evidence) -> tuple[Any, _SpyReaper]:
    from omnimarket.nodes.node_topic_reaper_compute.handlers.handler_topic_reaper import (
        HandlerTopicReaper,
    )
    from omnimarket.nodes.node_topic_reaper_scheduled_effect.handlers.handler_topic_reaper_scheduled import (
        HandlerTopicReaperScheduled,
    )

    spy = _SpyReaper(HandlerTopicReaper())
    handler = HandlerTopicReaperScheduled(
        environ=environ, reaper=spy, evidence=lambda: evidence, clock=lambda: T0
    )
    return handler, spy


def _run_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.getMessage().startswith("topic reaper run:")
    ]


# --- the contract level: a caller exists and the trigger check accepts both contracts -----------------------


def test_the_reaper_contract_names_a_caller_that_exists_and_names_it() -> None:
    callers = _contract(REAPER)["runtime_dispatch"].get("callers")
    assert callers, "node_topic_reaper_compute declares no caller"
    for entry in callers:
        text = (REPO / entry["path"]).read_text("utf-8")
        assert REAPER in text
        assert CALLER in entry["path"]


def test_the_trigger_check_passes_the_reaper_and_the_caller() -> None:
    contracts = tuple(
        f"src/omnimarket/nodes/{n}/contract.yaml" for n in (REAPER, CALLER)
    )
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(REPO), contracts=contracts)
    )
    assert result.findings == ()


def test_the_caller_subscribes_to_the_runtime_tick_and_declares_its_schedule() -> None:
    contract = _contract(CALLER)
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.intent.platform.runtime-tick.v1"
    ]
    schedule = contract["schedule"]
    assert schedule["job_id"] == "topic_reaper_dry_run"
    assert schedule["run_interval_seconds"] > 0
    assert (
        contract["input_subscriptions"][0]["operation"] == "topic_reaper.scheduled_tick"
    )


# --- the run: one line per run, the reaper called with the broker's observation, an injected clock -------------


def test_a_due_tick_calls_the_reaper_with_the_observation_and_logs_one_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    evidence = _Evidence((_quiet_topic("orphan.a.v1"), _quiet_topic("orphan.b.v1")))
    handler, spy = _handler(_overlay_root(tmp_path, policy=True), evidence)
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.now == T0
    assert {o.topic for o in request.observations} == {"orphan.a.v1", "orphan.b.v1"}
    assert request.boot_set_topics == ("declared.topic.v1",)
    lines = _run_lines(caplog)
    assert len(lines) == 1
    assert "flagged=2" in lines[0]
    assert "deletable=0" in lines[0]
    assert "dry_run=true" in lines[0]


def test_the_gate_runs_once_per_interval_and_state_carries_to_the_next_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    evidence = _Evidence((_quiet_topic("orphan.a.v1"),))
    handler, spy = _handler(_overlay_root(tmp_path, policy=True), evidence)
    assert handler.handle(tick(T0)) is not None
    assert handler.handle(tick(T0 + timedelta(seconds=1))) is None
    interval = _contract(CALLER)["schedule"]["run_interval_seconds"]
    assert handler.handle(tick(T0 + timedelta(seconds=interval))) is not None
    assert len(_run_lines(caplog)) == 2
    assert [len(r.state) for r in spy.requests] == [0, 1]


# --- no policy: the line is still written, nothing is read, nothing flagged ------------------------------------


def test_no_overlay_logs_the_missing_policy_and_reads_no_broker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    evidence = _Evidence((_quiet_topic("orphan.a.v1"),))
    handler, spy = _handler(_overlay_root(tmp_path, policy=False), evidence)
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert evidence.reads == 0
    assert spy.requests[0].policy is None
    assert spy.requests[0].observations == ()
    lines = _run_lines(caplog)
    assert len(lines) == 1
    assert REAPER_POLICY_MISSING in lines[0]
    assert "flagged=0" in lines[0]
    assert "deletable=0" in lines[0]


def test_an_unreadable_broker_is_a_failed_run_with_its_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from omnimarket.nodes.node_topic_reaper_scheduled_effect.protocols import (
        ReaperEvidenceError,
    )

    class _Down:
        def read(self) -> Any:
            raise ReaperEvidenceError("metrics-read-timeout")

    from omnimarket.nodes.node_topic_reaper_compute.handlers.handler_topic_reaper import (
        HandlerTopicReaper,
    )
    from omnimarket.nodes.node_topic_reaper_scheduled_effect.handlers.handler_topic_reaper_scheduled import (
        HandlerTopicReaperScheduled,
    )

    caplog.set_level(logging.INFO)
    spy = _SpyReaper(HandlerTopicReaper())
    handler = HandlerTopicReaperScheduled(
        environ=_overlay_root(tmp_path, policy=True),
        reaper=spy,
        evidence=lambda: _Down(),
        clock=lambda: T0,
    )
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    lines = _run_lines(caplog)
    assert len(lines) == 1
    assert "metrics-read-timeout" in lines[0]
    assert spy.requests == []


# --- never deletes -------------------------------------------------------------------------------------------


def test_an_enforcing_overlay_is_run_as_a_dry_run_and_hands_out_no_target(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    evidence = _Evidence((_quiet_topic("orphan.a.v1"),))
    handler, spy = _handler(
        _overlay_root(tmp_path, policy=True, enforce=True), evidence
    )
    handler.handle(tick(T0))
    handler.handle(tick(T0 + timedelta(days=2)))
    assert [r.policy.enforce for r in spy.requests if r.policy] == [False, False]
    lines = _run_lines(caplog)
    assert "deletable=1" in lines[1]
    assert "delete" not in lines[1].replace("deletable", "")


def test_the_caller_never_loads_or_names_the_delete_effect() -> None:
    for path in CALLER_DIR.rglob("*.py"):
        text = path.read_text("utf-8")
        assert "node_broker_topic_delete_effect" not in text, path
        assert "ModelReaperDeleteTarget" not in text, path
        assert "delete_targets" not in text, path
    probe = (
        "import sys;"
        f"import omnimarket.nodes.{CALLER}.handlers.handler_topic_reaper_scheduled;"
        "bad=[m for m in sys.modules if 'node_broker_topic_delete_effect' in m];"
        "sys.exit(1 if bad else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
