# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic harness trials retain eval identities and reject false passes."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.events.delegation_eval import ModelDelegationEvalRunCompleted
from omnimarket.handlers.handler_harness_eval import (
    ModelHarnessObservation,
    build_harness_run,
    summarize,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    eval_run_id,
)
from omnimarket.nodes.node_projection_delegation_eval.handlers.handler_projection_delegation_eval_run import (
    HandlerProjectionDelegationEvalRun,
)
from omnimarket.nodes.node_projection_delegation_eval.models.model_delegation_eval_run import (
    ModelDelegationEvalRunProjectionRequest,
)

pytestmark = pytest.mark.unit


def observation(item="a", **kwargs):
    return ModelHarnessObservation(
        item_key=item,
        task_class="docs-edit",
        harness="codex",
        model="test-model",
        configuration="default",
        duration_s=60,
        passed=True,
        harness_succeeded=True,
        **kwargs,
    )


def test_harness_identity_preserves_legacy_run_id_and_separates_engines():
    legacy = eval_run_id("m", "s", "v")
    assert legacy == UUID("bd0c4df7-367c-57bb-a361-68d52bcae1f9")
    assert legacy == eval_run_id("m", "s", "v", harness=None)
    assert eval_run_id(
        "m", "s", "v", harness="codex", model="same", configuration="a"
    ) != eval_run_id("m", "s", "v", harness="claude", model="same", configuration="a")
    assert eval_run_id(
        "m", "s", "v", harness="codex", model="same", configuration="a"
    ) != eval_run_id("m", "s", "v", harness="codex", model="same", configuration="b")


def test_checker_failure_overrides_successful_harness_and_projects_same_rows():
    row = observation().model_copy(
        update={"passed": False, "failure_kind": "checker-failed"}
    )
    run = build_harness_run(
        (row,),
        tenant_id=UUID(int=1),
        manifest_id="fixed-v1",
        observed_at=datetime(2026, 10, 26, tzinfo=UTC),
    )
    assert isinstance(run, ModelDelegationEvalRunCompleted)
    assert run.harness == "codex"
    assert run.item_verdicts[0].replayed_verdict == "refused"
    assert run.item_verdicts[0].duration_s == 60
    assert run.item_verdicts[0].failure_kind == "checker-failed"
    recorded = next(r for r in run.results if r.arm == "recorded")
    assert recorded.false_pass_count == 1
    assert next(r for r in run.results if r.arm == "replayed").false_pass_count == 0
    projected = HandlerProjectionDelegationEvalRun().handle(
        ModelDelegationEvalRunProjectionRequest.model_validate(run.model_dump())
    )
    assert projected.verdict_rows[0].harness == "codex"
    assert projected.result_rows[0].configuration == "default"


def test_summary_separates_unavailable_and_uses_median_minutes():
    rows = (
        observation("a"),
        observation("b").model_copy(
            update={"duration_s": 180, "passed": False, "failure_kind": "timeout"}
        ),
        observation("c").model_copy(
            update={
                "available": False,
                "passed": False,
                "failure_kind": "not-logged-in",
            }
        ),
    )
    (cell,) = summarize(rows)
    assert cell["n"] == 2
    assert cell["unavailable"] == 1
    assert cell["pass_rate"] == 0.5
    assert cell["median_minutes"] == 2
    assert cell["failure_kinds"] == {"timeout": 1, "not-logged-in": 1}


def test_mixed_identity_and_duplicate_items_are_refused():
    for rows in (
        (observation(), observation()),
        (observation(), observation("b").model_copy(update={"harness": "claude"})),
    ):
        with pytest.raises(ValueError, match=r"duplicate item|one harness"):
            build_harness_run(
                rows,
                tenant_id=UUID(int=1),
                manifest_id="fixed",
                observed_at=datetime.now(UTC),
            )


def test_capability_projection_reuses_bounds_and_never_pools_harnesses():
    from omnimarket.handlers.handler_harness_eval import build_harness_capability_run
    from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_projection_delegation_capability_map import (
        HandlerProjectionDelegationCapabilityMap,
    )

    rows = tuple(observation(str(i)) for i in range(3))
    rows += tuple(r.model_copy(update={"harness": "claude-glm"}) for r in rows)
    rows += (
        observation("unavailable").model_copy(
            update={"available": False, "passed": False}
        ),
    )
    run = build_harness_capability_run(
        rows,
        tenant_id=UUID(int=1),
        observed_at=datetime.now(UTC),
        suite_revision="fixed-v1",
        harness_revision="adapter-sha",
    )
    result = HandlerProjectionDelegationCapabilityMap().handle(run)
    assert len(result.cells) == 2
    assert result.folded_row_count == 6
    assert len({c.backend_id for c in result.cells}) == 2
    assert all(
        c.n == 3 and c.pass_rate == 1 and str(c.verdict) == "REFUSED"
        for c in result.cells
    )
    assert result.best_configurations == ()


@pytest.mark.parametrize("cause", ["Read-only file system", "Operation not permitted"])
def test_startup_failure_is_unavailable_but_tool_refusal_is_an_attempt(cause):
    from omnimarket.handlers.handler_harness_eval import normalize_observation

    failed = observation().model_copy(
        update={
            "passed": False,
            "harness_succeeded": False,
            "failure_kind": "harness-error",
        }
    )
    startup = normalize_observation(
        failed,
        diagnostic=f"Error: failed to initialize in-process app-server client: {cause}",
    )
    assert not startup.available
    assert startup.failure_kind == "sandbox-initialization"
    refusal = normalize_observation(
        failed, transcript='{"type":"system","subtype":"permission_denied"}'
    )
    assert refusal.available
    assert refusal.failure_kind == "permission-denied"
    assert normalize_observation(
        observation(), transcript='{"subtype":"permission_denied"}'
    ).passed


def test_projection_writer_persists_identity_and_checker_metadata_on_redelivery():
    from tests.test_golden_chain_projection_delegation_eval import _run_writer

    writer, store = _run_writer()
    failed = observation().model_copy(
        update={"passed": False, "failure_kind": "checker-failed"}
    )
    run = build_harness_run(
        (failed,),
        tenant_id=UUID(int=1),
        manifest_id="fixed-v2",
        observed_at=datetime(2026, 10, 26, tzinfo=UTC),
    )
    first = writer.handle(run.model_dump(mode="json"))
    assert first["rows_upserted"] == 5
    [verdict] = store.tables["delegation_eval_item_verdicts"].values()
    assert (verdict["harness"], verdict["model"], verdict["configuration"]) == (
        "codex",
        "test-model",
        "default",
    )
    assert (verdict["duration_s"], verdict["failure_kind"]) == (60, "checker-failed")
    assert all(
        row["harness"] == "codex"
        for row in store.tables["delegation_eval_results"].values()
    )
    repeated = writer.handle(run.model_dump(mode="json"))
    assert repeated["rows_upserted"] == 0
    assert repeated["rows_refused_by_ordering_guard"] == 5
