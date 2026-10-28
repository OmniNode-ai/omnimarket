# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the committed h201 measurement without launching a subject model."""

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.handlers.handler_harness_eval import (
    build_harness_capability_run,
    build_harness_run,
    summarize,
)
from omnimarket.models.delegation.model_harness_observation import (
    ModelHarnessObservation,
)
from omnimarket.models.model_engine_experiment import (
    ModelEngineExperimentRow,
    format_experiment_line,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_projection_delegation_capability_map import (
    HandlerProjectionDelegationCapabilityMap,
)
from tests.harness_evals.fixtures import TASK_TYPES, fixed_tasks, fixture_digest
from tests.harness_evals.recent import recent_digest, recent_tasks

pytestmark = pytest.mark.unit

RESULTS = (
    Path(__file__).resolve().parents[3]
    / "benchmarks/harness_evals/results/2026-10-27-h201-codex"
)


@pytest.mark.parametrize(
    ("cohort", "n", "offset"), [("fixed", 3, 0), ("recent", 1, 21)]
)
def test_recorded_codex_measurement_replays(cohort: str, n: int, offset: int) -> None:
    directory = RESULTS / cohort

    def read(name: str) -> Any:
        return json.loads((directory / name).read_text())

    observations = tuple(
        ModelHarnessObservation.model_validate(row) for row in read("observations.json")
    )
    tasks = fixed_tasks() if cohort == "fixed" else recent_tasks()
    digest = fixture_digest() if cohort == "fixed" else recent_digest()
    configuration = "fixture-v1" if cohort == "fixed" else "receipt-v1"
    assert {row.item_key for row in observations} == {t["item_key"] for t in tasks}
    assert len(observations) == n * len(TASK_TYPES)
    assert Counter(row.task_class for row in observations) == dict.fromkeys(
        TASK_TYPES, n
    )
    assert all(
        row.available
        and row.harness == "codex"
        and row.model == "configured-default"
        and row.configuration == configuration + ":effort-low:timeout-90s:adapter-cli"
        and row.duration_s > 0
        for row in observations
    )
    for row in observations:
        assert row.passed == (row.task_class != "rebase-conflict")
        assert row.failure_kind == (None if row.passed else "checker-failed")
    assert read("summary.json") == summarize(observations)

    proofs = read("source-proofs.json")
    harness_revision = hashlib.sha256(
        (RESULTS / "harness-versions.json").read_bytes()
    ).hexdigest()
    assert proofs["cohort"] == cohort
    assert proofs["fixture_sha256"] == digest
    assert proofs["harness_versions_sha256"] == harness_revision
    assert proofs["excluded_trials"] == []

    events = read("eval-runs.json")
    assert len(events) == 1
    event = events[0]
    observed_at = datetime.fromisoformat(event["observed_at"])
    tenant_id = UUID(event["tenant_id"])
    prefix = "harness-lanes-v2:" if cohort == "fixed" else "harness-lanes-recent-v1:"
    replay = build_harness_run(
        observations,
        tenant_id=tenant_id,
        manifest_id=prefix + digest,
        observed_at=observed_at,
    )
    assert replay.model_dump(mode="json") == event

    capability = build_harness_capability_run(
        observations,
        tenant_id=tenant_id,
        observed_at=observed_at,
        suite_revision=digest,
        harness_revision=harness_revision,
    )
    assert capability.model_dump(mode="json") == read("capability-map-run.json")
    projection = HandlerProjectionDelegationCapabilityMap().handle(capability)
    assert projection.model_dump(mode="json") == read("capability-cells.json")
    assert not projection.best_configurations
    assert all(
        cell["verdict"] == "REFUSED" for cell in read("capability-cells.json")["cells"]
    )

    experiments = read("experiment-rows.json")
    assert len(experiments) == len(observations)
    proposed = []
    for index, (observation, recorded) in enumerate(
        zip(observations, experiments, strict=True), start=offset + 1
    ):
        expected = ModelEngineExperimentRow(
            lane="fill-exp-harness-evals-codex-glm-20261027T1957Z",
            ticket="OMN-21396",
            attempt=index,
            engine="codex",
            model=observation.model,
            outcome="done" if observation.passed else "failed",
            failure_cause=observation.failure_kind or "none",
            duration_s=round(observation.duration_s),
            next_engine="none",
            harness=observation.harness,
            task_class=observation.task_class,
            configuration=observation.configuration,
            item_key=observation.item_key,
        )
        assert recorded == expected.model_dump(mode="json")
        proposed.append(
            format_experiment_line(expected, observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        )
    assert (directory / "experiment-status.txt").read_text().splitlines() == proposed
