# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start-path census and refusal drill (OMN-22940).

The drill scenarios run the real start-admission effect over the real in-memory gate store: a stop open on the piece is
the bypassing start, and the census reads the answer the path gave.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_start_admission import (
    EnumAdmissionMode,
    EnumAdmissionOutcome,
    ModelStartAdmissionPolicy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_start_admission_effect.handlers.adapter_log_in_memory import (
    InMemoryStartAdmissionLog,
)
from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
)
from omnimarket.nodes.node_start_admission_effect.tests.test_start_admission import (
    STORE_POLICY,
    policy,
    stopped_store,
)
from omnimarket.nodes.node_start_path_census_compute import (
    NodeStartPathCensusCompute,
)
from omnimarket.nodes.node_start_path_census_compute.drill import (
    plant_bypassing_start,
)
from omnimarket.nodes.node_start_path_census_compute.handler import (
    HandlerStartPathCensus,
)
from omnimarket.nodes.node_start_path_census_compute.models import (
    TAG_DRILL,
    EnumDrillVerdict,
    EnumStartPathStatus,
    ModelDrillObservation,
    ModelObservedStart,
    ModelStartPathCensusRequest,
    ModelStartPathCensusResult,
    ModelStartPathEntry,
)
from omnimarket.nodes.node_start_path_census_compute.overlay import (
    load_start_path_census_policy,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

OBSERVE, ENFORCE = EnumAdmissionMode.OBSERVE, EnumAdmissionMode.ENFORCE
RUNNER = ModelStartPathEntry(
    path_id="lane_runner", status=EnumStartPathStatus.LIVE, binding="runner.start"
)
HOOK = ModelStartPathEntry(
    path_id="agent_hook", status=EnumStartPathStatus.LIVE, binding="hook.pre_agent"
)
RETIRED = ModelStartPathEntry(
    path_id="old_cron", status=EnumStartPathStatus.DEAD, reason="timer uninstalled"
)


def census(
    entries: tuple[ModelStartPathEntry, ...] = (RUNNER, HOOK, RETIRED),
    admission: ModelStartAdmissionPolicy | None = None,
    starts: tuple[ModelObservedStart, ...] = (),
    drills: tuple[ModelDrillObservation, ...] = (),
) -> ModelStartPathCensusResult:
    return HandlerStartPathCensus().handle(
        ModelStartPathCensusRequest(
            correlation_id="census-1",
            entries=entries,
            admission_policy=admission or policy(OBSERVE),
            observed_starts=starts,
            drills=drills,
        )
    )


def drill_on(
    store: InMemoryGateStore, path: str, mode: EnumAdmissionMode, key: str = "k1"
) -> ModelDrillObservation:
    admission = HandlerStartAdmission(
        store, InMemoryStartAdmissionLog(), policy(mode, {path: mode})
    )
    return plant_bypassing_start(
        admission.handle, path_id=path, piece="alpha", run_key=key, now=1.0
    )


# ---- AC1: the census ----


def test_census_reads_every_listed_path_live_with_a_binding_or_dead() -> None:
    result = census(starts=(ModelObservedStart(path_id="lane_runner"),))

    by_path = {reading.path_id: reading for reading in result.paths}
    assert result.passed is True
    assert (by_path["lane_runner"].status, by_path["lane_runner"].binding) == (
        EnumStartPathStatus.LIVE,
        "runner.start",
    )
    assert by_path["old_cron"].status is EnumStartPathStatus.DEAD
    assert by_path["old_cron"].drill is EnumDrillVerdict.NOT_APPLICABLE
    assert result.unlisted_starts == ()
    assert result.dead_path_starts == ()


def test_census_an_unlisted_start_fails_the_census() -> None:
    result = census(
        starts=(
            ModelObservedStart(path_id="lane_runner"),
            ModelObservedStart(path_id="scratch_script"),
        )
    )

    assert result.passed is False
    assert result.unlisted_starts == ("scratch_script",)


def test_census_a_start_on_a_dead_path_fails_the_census() -> None:
    result = census(starts=(ModelObservedStart(path_id="old_cron"),))

    assert result.passed is False
    assert result.dead_path_starts == ("old_cron",)


def test_census_refuses_a_live_path_with_no_binding_a_dead_one_with_no_reason_and_a_repeat() -> (
    None
):
    with pytest.raises(ValidationError, match="names no binding"):
        ModelStartPathEntry(path_id="p", status=EnumStartPathStatus.LIVE)
    with pytest.raises(ValidationError, match="names no reason"):
        ModelStartPathEntry(path_id="p", status=EnumStartPathStatus.DEAD)
    with pytest.raises(ValidationError, match="more than once"):
        census(entries=(RUNNER, RUNNER))


def test_census_paths_come_from_the_overlay_and_a_missing_overlay_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_start_path_census_policy()

    install_node_overlay(
        monkeypatch,
        tmp_path,
        "start_path_census",
        {
            "paths": [
                {"path_id": "lane_runner", "status": "live", "binding": "runner.start"},
                {
                    "path_id": "old_cron",
                    "status": "dead",
                    "reason": "timer uninstalled",
                },
            ]
        },
    )
    loaded = load_start_path_census_policy()

    assert census(entries=loaded.paths).passed is True
    assert [entry.path_id for entry in loaded.paths] == ["lane_runner", "old_cron"]


def test_census_contract_names_its_node_handler_and_topics() -> None:
    contract = yaml.safe_load(NodeStartPathCensusCompute.contract_path.read_text())

    assert contract["name"] == "node_start_path_census_compute"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in (contract["event_bus"]["subscribe_topics"])
    )
    result = NodeStartPathCensusCompute().handle(
        ModelStartPathCensusRequest(
            correlation_id="c",
            entries=(RUNNER,),
            admission_policy=policy(OBSERVE),
        )
    )
    assert result.correlation_id == "c"


# ---- AC2: the drill ----


def test_drill_a_bypassing_start_is_refused_only_when_enforcement_is_enforce() -> None:
    observed = drill_on(stopped_store(), "lane_runner", OBSERVE)
    enforced = drill_on(stopped_store(), "lane_runner", ENFORCE)

    assert (observed.outcome, observed.proceed) == (
        EnumAdmissionOutcome.OBSERVED_REFUSAL,
        True,
    )
    assert (enforced.outcome, enforced.proceed) == (
        EnumAdmissionOutcome.REFUSED,
        False,
    )
    in_observe = census(
        admission=policy(OBSERVE, {"lane_runner": OBSERVE}), drills=(observed,)
    )
    in_enforce = census(
        admission=policy(OBSERVE, {"lane_runner": ENFORCE}), drills=(enforced,)
    )
    assert in_observe.passed is True
    assert in_observe.paths[0].drill is EnumDrillVerdict.OBSERVING
    assert in_enforce.passed is True
    assert in_enforce.paths[0].drill is EnumDrillVerdict.PASS


def test_drill_a_path_that_refuses_in_observe_or_lets_the_bypass_through_in_enforce_fails() -> (
    None
):
    refused_in_observe = ModelDrillObservation(
        path_id="lane_runner",
        mode=OBSERVE,
        outcome=EnumAdmissionOutcome.REFUSED,
        proceed=False,
    )
    admitted_in_enforce = drill_on(
        InMemoryGateStore(STORE_POLICY), "lane_runner", ENFORCE
    )

    in_observe = census(
        admission=policy(OBSERVE, {"lane_runner": OBSERVE}),
        drills=(refused_in_observe,),
    )
    in_enforce = census(
        admission=policy(OBSERVE, {"lane_runner": ENFORCE}),
        drills=(admitted_in_enforce,),
    )

    assert admitted_in_enforce.outcome is EnumAdmissionOutcome.ADMITTED
    assert in_observe.passed is False
    assert in_observe.drill_failures == ("lane_runner:refused_in_observe",)
    assert in_enforce.passed is False
    assert in_enforce.drill_failures == ("lane_runner:bypass_not_refused:admitted",)
    assert in_enforce.paths[0].drill is EnumDrillVerdict.FAIL


def test_drill_a_drill_run_in_another_mode_than_the_path_declares_fails() -> None:
    ran_in_observe = drill_on(stopped_store(), "lane_runner", OBSERVE)

    result = census(
        admission=policy(OBSERVE, {"lane_runner": ENFORCE}), drills=(ran_in_observe,)
    )

    assert result.passed is False
    assert "lane_runner:mode_mismatch:observe!=enforce" in result.drill_failures


def test_drill_an_enforced_path_with_no_drill_fails_and_an_observed_one_is_not_run() -> (
    None
):
    enforced = census(admission=policy(OBSERVE, {"lane_runner": ENFORCE}))
    observed = census()

    assert enforced.passed is False
    assert enforced.drill_failures == ("lane_runner:enforce_without_drill",)
    assert observed.passed is True
    assert {r.path_id: r.drill for r in observed.paths} == {
        "lane_runner": EnumDrillVerdict.NOT_RUN,
        "agent_hook": EnumDrillVerdict.NOT_RUN,
        "old_cron": EnumDrillVerdict.NOT_APPLICABLE,
    }


# ---- AC3: a request tagged drill never suppresses an operational failure ----


def test_drill_tag_a_passing_drill_does_not_clear_an_operational_failure_on_its_path() -> (
    None
):
    passing = drill_on(stopped_store(), "lane_runner", ENFORCE)
    admission = policy(OBSERVE, {"lane_runner": ENFORCE})
    failing_start = ModelObservedStart(path_id="lane_runner", failure="engine_crashed")

    clean = census(admission=admission, drills=(passing,))
    with_failure = census(
        admission=admission, starts=(failing_start,), drills=(passing,)
    )

    assert passing.tag == TAG_DRILL
    assert clean.passed is True
    assert clean.operational_failures == ()
    assert with_failure.paths[0].drill is EnumDrillVerdict.PASS
    assert with_failure.passed is False
    assert with_failure.operational_failures == ("lane_runner:engine_crashed",)


def test_drill_tag_a_drill_tag_is_no_exemption_for_an_unlisted_start_or_a_dead_path() -> (
    None
):
    tagged = {"tag": TAG_DRILL, "instance": "test"}

    result = census(
        starts=(
            ModelObservedStart(path_id="scratch_script", **tagged),
            ModelObservedStart(path_id="old_cron", **tagged),
        )
    )

    assert result.passed is False
    assert result.unlisted_starts == ("scratch_script",)
    assert result.dead_path_starts == ("old_cron",)


def test_drill_tag_a_tag_off_the_test_instance_is_an_operational_failure() -> None:
    off_test_start = ModelObservedStart(
        path_id="lane_runner", tag=TAG_DRILL, instance="prod"
    )
    off_test_drill = ModelDrillObservation(
        path_id="lane_runner",
        instance="prod",
        mode=OBSERVE,
        outcome=EnumAdmissionOutcome.OBSERVED_REFUSAL,
        proceed=True,
    )

    result = census(starts=(off_test_start,), drills=(off_test_drill,))

    assert result.passed is False
    assert result.operational_failures == (
        "lane_runner:drill_tag_off_test_instance",
        "lane_runner:drill_result_not_on_test_instance",
    )
    assert result.paths[0].drill is EnumDrillVerdict.NOT_RUN


def test_drill_tag_a_failure_carried_by_a_drill_start_stays_a_drill_finding_beside_an_operational_one() -> (
    None
):
    result = census(
        starts=(
            ModelObservedStart(
                path_id="lane_runner",
                tag=TAG_DRILL,
                instance="test",
                failure="planted_fault",
            ),
            ModelObservedStart(path_id="lane_runner", failure="engine_crashed"),
        )
    )

    assert result.passed is False
    assert result.drill_failures == ("lane_runner:drill_start_failure:planted_fault",)
    assert result.operational_failures == ("lane_runner:engine_crashed",)
