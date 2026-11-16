# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the delegation history compute tests: a policy, backends, and readings shaped like the lab projection's."""

from __future__ import annotations

from omnimarket.models.floor_history_request import (
    ModelArmRule,
    ModelAttemptCounts,
    ModelBackendSettings,
    ModelEngineRule,
    ModelHistoryPolicy,
    ModelHistoryRequest,
    ModelImportedArm,
    ModelImportedBaseline,
    ModelImportedExperiment,
    ModelImportedPaired,
    ModelLatencyRead,
    ModelSeenRead,
    ModelServingDeclaration,
)

NOW = "2026-11-14T02:30:00Z"
LOCAL_HOST = "192.0.2.10"
SECOND_HOST = "192.0.2.11"

POLICY = ModelHistoryPolicy(
    engine_rules=(
        ModelEngineRule(kind="local", backend_prefixes=("local-", "eval-local-")),
        ModelEngineRule(
            kind="GLM", backend_prefixes=("harness:glm",), model_prefixes=("glm",)
        ),
        ModelEngineRule(
            kind="Claude",
            backend_prefixes=("harness:claude:",),
            model_prefixes=("claude",),
        ),
        ModelEngineRule(
            kind="Codex",
            backend_prefixes=("harness:codex",),
            model_prefixes=("gpt-", "codex"),
        ),
    ),
    fallback_engine="other",
    not_a_model_backend_ids=("not routed",),
    serving=(
        ModelServingDeclaration(
            backend_id="eval-local-b",
            quantization="Q3_K Medium",
            thinking="off",
            recorded_in="routing overlay comment",
        ),
        ModelServingDeclaration(
            backend_id="local-a", thinking="on", recorded_in="vllm unit"
        ),
    ),
    arm_rules=(
        ModelArmRule(label_pattern="^local_a", backend_id="local-a"),
        ModelArmRule(label_pattern="^local_b", backend_id="eval-local-b"),
        ModelArmRule(label_pattern="^ghost", backend_id="no-such-backend"),
    ),
    window_days=14,
)

BACKENDS = (
    ModelBackendSettings(
        backend_id="local-a",
        model_name="Model-X",
        host=LOCAL_HOST,
        context_tokens=131072,
    ),
    ModelBackendSettings(
        backend_id="eval-local-b",
        model_name="Model-X",
        host=SECOND_HOST,
        context_tokens=65536,
        price_in_usd_per_mtok=0.0,
        price_out_usd_per_mtok=0.0,
    ),
    ModelBackendSettings(
        backend_id="harness:glm:flash",
        model_name="glm-flash",
        price_in_usd_per_mtok=0.5,
        price_out_usd_per_mtok=2.0,
    ),
)


def attempts(
    bucket: str,
    *,
    n: int,
    accepted: int,
    timeouts: int = 0,
    model: str = "Model-X",
    backend: str = "local-a",
    host: str = LOCAL_HOST,
    task: str = "document",
    cost: float = 0.0,
) -> ModelAttemptCounts:
    return ModelAttemptCounts(
        bucket=bucket,
        model_name=model,
        backend_id=backend,
        host=host,
        task_type=task,
        attempts=n,
        accepted=accepted,
        timeouts=timeouts,
        cost_usd=cost,
    )


def latency(
    bucket: str,
    values: tuple[int, ...],
    *,
    model: str = "Model-X",
    backend: str = "local-a",
    host: str = LOCAL_HOST,
    task: str = "document",
) -> ModelLatencyRead:
    return ModelLatencyRead(
        bucket=bucket,
        model_name=model,
        backend_id=backend,
        host=host,
        task_type=task,
        latencies_ms=values,
    )


def seen(backend: str, model: str, host: str, first: str, last: str) -> ModelSeenRead:
    return ModelSeenRead(
        model_name=model,
        backend_id=backend,
        host=host,
        first_seen=first,
        last_seen=last,
    )


def experiment(**changes: object) -> ModelImportedExperiment:
    base: dict[str, object] = {
        "id": "exp-demo",
        "question": "does it help?",
        "source": "h-test:exp-demo-2026-11-13T2045Z.md",
        "task_set_description": "truth set",
        "task_set_hash": "a" * 64,
        "baseline": ModelImportedBaseline(label="majority", correct=30, n=156),
        "arms": (
            ModelImportedArm(
                label="local_a zero",
                correct=114,
                n=156,
                p50_latency_s=0.58,
                gpu_seconds=89.8,
            ),
            ModelImportedArm(
                label="local_b few",
                correct=130,
                n=156,
                p50_latency_s=1.46,
                gpu_seconds=235.3,
            ),
            ModelImportedArm(label="ghost arm", correct=1, n=2),
            ModelImportedArm(label="plain arm", correct=2, n=2),
        ),
        "paired": (
            ModelImportedPaired(
                test="exact McNemar",
                arm_a="local_a zero",
                arm_b="local_b few",
                only_a_right=7,
                only_b_right=23,
                p_value=0.0052,
            ),
        ),
        "verdict": "Result: few-shot lifts it",
        "date": "2026-11-13T20:45:00Z",
        "date_source": "filename",
    }
    base.update(changes)
    return ModelImportedExperiment.model_validate(base)


def request(**changes: object) -> ModelHistoryRequest:
    base: dict[str, object] = {
        "now": NOW,
        "policy": POLICY,
        "backends": BACKENDS,
        "attempts": (
            attempts("2026-11-13T10:00:00Z", n=10, accepted=8, timeouts=1, cost=0.5),
            attempts(
                "2026-11-13T11:00:00Z",
                n=10,
                accepted=5,
                timeouts=2,
                task="review",
                cost=0.25,
            ),
            attempts("2026-11-14T01:00:00Z", n=4, accepted=4),
            attempts(
                "2026-11-14T01:00:00Z",
                n=3,
                accepted=1,
                model="glm-flash",
                backend="harness:glm:flash",
                host="client-1",
            ),
            attempts(
                "2026-11-14T01:00:00Z",
                n=2,
                accepted=0,
                model="glm-flash",
                backend="harness:glm:flash",
                host="client-2",
            ),
            attempts(
                "2026-11-14T01:00:00Z",
                n=9,
                accepted=0,
                model="not routed (timeout)",
                backend="not routed",
                host="not routed",
            ),
        ),
        "latencies": (
            latency(
                "2026-11-13T10:00:00Z",
                (100, 200, 300, 400, 500, 600, 700, 800, 900, 1000),
            ),
            latency("2026-11-14T01:00:00Z", (50,), backend="local-a"),
        ),
        "seen": (
            seen(
                "local-a",
                "Model-X",
                LOCAL_HOST,
                "2026-09-01T00:00:00Z",
                "2026-11-14T01:00:00Z",
            ),
            seen(
                "eval-local-b",
                "Model-X",
                SECOND_HOST,
                "2026-11-01T00:00:00Z",
                "2026-11-13T00:00:00Z",
            ),
            seen(
                "harness:glm:flash",
                "glm-flash",
                "client-1",
                "2026-10-01T00:00:00Z",
                "2026-11-14T00:00:00Z",
            ),
            seen(
                "not routed",
                "not routed (timeout)",
                "not routed",
                "2026-08-01T00:00:00Z",
                "2026-11-14T00:00:00Z",
            ),
        ),
        "experiments": (experiment(),),
        "sources": (),
    }
    base.update(changes)
    return ModelHistoryRequest.model_validate(base)
