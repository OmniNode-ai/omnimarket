# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract, integration mapping, and Temporal runner-read coverage."""

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from importlib import resources
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.model_merged_change_reading import (
    ModelMcrNodeReading,
    ModelMcrReading,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.handler_runner_reading_integration import (
    HandlerRunnerReadingIntegration,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read import (
    ModelMergeFacts,
    ModelRunnerIntegrationConfig,
    ModelRunnerIntegrationRequest,
    ModelRunnerLineOutcome,
    ModelRunnerReadPlan,
    ModelRunnerReadPublishInput,
    ModelRunnerReadSteps,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.runtime_runner_read import (
    build_plan,
    contract_config,
)

pytestmark = pytest.mark.unit

AS_OF = datetime(2026, 11, 17, 12, tzinfo=UTC)
READ_AT = datetime(2026, 11, 17, 12, 1, tzinfo=UTC)


def reading(number=1, **overrides):
    values = {
        "repo": "omnimarket",
        "number": number,
        "merge_sha": "abc123",
        "kind": "topic",
        "in_build": True,
        "nodes": (),
        "last_run_at": AS_OF,
        "last_result": "ok",
        "verdict": "SEEN_RUNNING",
        "missing_step": "",
        "next_action": "",
        "detail": "reader detail",
    }
    values.update(overrides)
    return ModelMcrReading(**values)


def outcome(number, value=None, cause=""):
    return ModelRunnerLineOutcome(
        repo="omnimarket",
        number=number,
        reading=value,
        unread_cause=cause,
        read_at=READ_AT,
    )


def map_outcomes(outcomes, **kwargs):
    return HandlerRunnerReadingIntegration().handle(
        ModelRunnerIntegrationRequest(
            as_of=AS_OF,
            origin="test:runner",
            outcomes=tuple(outcomes),
            config=ModelRunnerIntegrationConfig.model_validate(
                contract_config()["integration"]
            ),
            **kwargs,
        )
    )


def test_contract_maps_every_reader_missing_step():
    config = ModelRunnerIntegrationConfig.model_validate(
        contract_config()["integration"]
    )
    text = (
        resources.files("omnimarket.nodes.node_merged_change_reading_compute")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    reader_config = yaml.safe_load(text)["merged_change_reading"]
    missing_steps = {item["missing_step"] for item in reader_config["requirements"]}
    missing_steps.add(reader_config["no_runner_step"])
    assert missing_steps <= config.missing_steps.keys()


def test_running_paths_and_read_time():
    node = ModelMcrNodeReading(
        node="node_x",
        loaded=True,
        subscribed_topics=(),
        last_result_topic="result",
        last_run_at=AS_OF,
    )
    result = map_outcomes(
        [
            outcome(1, reading(1, nodes=(node,))),
            outcome(2, reading(2, kind="overlay_value")),
        ]
    )
    topic, overlay = result.batch.readings
    assert (topic.state, topic.gap_class, topic.running_path) == (
        "running",
        "none",
        "node_x",
    )
    assert "SEEN_RUNNING" in topic.running_line
    assert "last_run_at=" in topic.running_line
    assert topic.read_at == READ_AT
    assert overlay.running_path == "overlay_store_checkout"


def test_not_running_rules_and_unit_target():
    action = "add the subscription"
    result = map_outcomes(
        [
            outcome(
                1,
                reading(
                    1,
                    verdict="NOT_RUNNING",
                    missing_step="no_trigger_subscription",
                    next_action=action,
                ),
            ),
            outcome(
                6051,
                reading(
                    6051,
                    verdict="NOT_RUNNING",
                    missing_step="last_run_failed",
                    next_action="fix the unit",
                ),
            ),
            outcome(
                3,
                reading(
                    3,
                    verdict="NOT_RUNNING",
                    missing_step="no_declared_runner",
                    next_action="declare runner",
                ),
            ),
        ],
        unit_targets={"omnimarket#6051": "onex-host-drift.service on h051"},
    )
    registration, failed, undeclared = result.batch.readings
    assert (registration.state, registration.gap_class, registration.step) == (
        "not_running",
        "needs_registration",
        action,
    )
    assert (failed.state, failed.gap_class, failed.target) == (
        "defect",
        "needs_unit",
        "onex-host-drift.service on h051",
    )
    assert undeclared.gap_class == "needs_reader"


def test_unread_and_library_mappings():
    result = map_outcomes(
        [
            outcome(1, cause="RuntimeError: boom"),
            outcome(2, reading(2, kind="library", verdict="UNREAD", in_build=True)),
            outcome(3, reading(3, kind="route", verdict="UNREAD")),
        ]
    )
    failed, library, route = result.batch.readings
    assert (failed.state, failed.gap_class) == ("unproven", "needs_reading")
    assert "boom" in failed.step
    assert failed.state != "running"
    assert library.state == "library"
    assert (route.state, route.gap_class) == ("unproven", "needs_reading")


def test_merge_facts_counts_and_batch_provenance():
    merged_at = datetime(2026, 11, 16, tzinfo=UTC)
    result = map_outcomes(
        [
            outcome(1, reading(1)),
            outcome(
                2,
                reading(
                    2,
                    verdict="NOT_RUNNING",
                    missing_step="not_in_build",
                    next_action="roll dev",
                ),
            ),
            outcome(3, reading(3, verdict="UNREAD", kind="route")),
            outcome(4, cause="unavailable"),
        ],
        merge_facts={
            "omnimarket#1": ModelMergeFacts(
                title="A merged change", merged_at=merged_at
            )
        },
    )
    known, unknown = result.batch.readings[:2]
    assert (known.title, known.merged_at) == ("A merged change", merged_at)
    assert "(merge commit unread)" in unknown.title
    assert unknown.merged_at == AS_OF
    assert result.counts.model_dump() == {
        "declared": 4,
        "seen_running": 1,
        "not_running": 1,
        "unread": 2,
    }
    assert all(
        item.read_at == READ_AT and item.origin == "test:runner"
        for item in result.batch.readings
    )


def test_build_plan_expands_python_and_publish_flag():
    cfg = contract_config()
    plan = build_plan(cfg)
    for name in ("lines", "read_line", "publish"):
        template = cfg["steps"][name]
        assert "{python}" in template
        assert getattr(plan.steps, name) == tuple(
            sys.executable if part == "{python}" else part for part in template
        )
    assert plan.publish is True
    assert build_plan(cfg, publish=False).publish is False


@pytest.mark.asyncio
async def test_workflow_retries_unread_lines_and_publishes_in_order():
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_runner_read import (
        ACTIVITY_NAME,
        MergedChangeReadingWorkflow,
    )

    calls = Counter()

    @activity.defn(name=ACTIVITY_NAME)
    async def fake_step(argv: list[str], stdin_text: str) -> str:
        if argv[-1] == "lines":
            return json.dumps(["omnimarket#1", "omnimarket#2", "omnimarket#3"])
        if "--change" in argv:
            key = argv[argv.index("--change") + 1]
            calls[key] += 1
            if key == "omnimarket#3":
                raise RuntimeError("ssh refused")
            number = int(key.split("#")[1])
            verdict = "SEEN_RUNNING" if number == 1 else "NOT_RUNNING"
            return json.dumps(
                {
                    "reading": {
                        "readings": [
                            reading(number, verdict=verdict).model_dump(mode="json")
                        ]
                    }
                }
            )
        assert argv[-1] == "publish"
        return stdin_text

    plan = ModelRunnerReadPlan(
        steps=ModelRunnerReadSteps(
            lines=("x", "lines"), read_line=("x", "reader"), publish=("x", "publish")
        ),
        line_concurrency=2,
        step_timeout_seconds=30,
        step_max_attempts=2,
    )
    queue = f"runner-read-test-{uuid4()}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=queue,
            workflows=[MergedChangeReadingWorkflow],
            activities=[fake_step],
        ),
    ):
        result = await env.client.execute_workflow(
            MergedChangeReadingWorkflow.run,
            plan.model_dump_json(),
            id=f"runner-read-{uuid4()}",
            task_queue=queue,
        )
    payload = ModelRunnerReadPublishInput.model_validate_json(result)
    assert [item.key for item in payload.outcomes] == [
        "omnimarket#1",
        "omnimarket#2",
        "omnimarket#3",
    ]
    assert payload.outcomes[0].reading.verdict == "SEEN_RUNNING"
    assert payload.outcomes[2].reading is None
    assert "ssh refused" in payload.outcomes[2].unread_cause
    assert calls == {"omnimarket#1": 1, "omnimarket#2": 1, "omnimarket#3": 2}
