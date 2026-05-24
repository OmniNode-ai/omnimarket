# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
import json
import uuid as uuid_mod
from typing import Any

from omnimarket.nodes.node_swarm_decomposer_compute.models.enums import (
    EnumDecompositionStatus,
    EnumSubtaskCategory,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_decomposition import (
    ModelDecomposition,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_swarm_decompose_request import (
    ModelSwarmDecomposeRequest,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_swarm_decompose_result import (
    ModelSwarmDecomposeResult,
)


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.encode()).hexdigest()[:16]


def _passthrough_subtask(task: str, task_hash: str) -> ModelSubtask:
    subtask_id = ModelSubtask.make_subtask_id(task_hash, 0, task)
    return ModelSubtask(
        subtask_id=subtask_id,
        description=task,
        model_affinity="",
        depends_on=(),
        estimated_tokens=0,
        token_estimation_method="char_ratio",
        category=EnumSubtaskCategory.GENERAL,
    )


def _parse_subtasks(
    raw_json: str,
    original_task_hash: str,
    endpoint_ids: list[str],
) -> list[ModelSubtask]:
    data: dict[str, Any] = json.loads(raw_json)
    raw_subtasks: list[dict[str, Any]] = data.get("subtasks", [])

    subtask_ids: list[str] = [
        ModelSubtask.make_subtask_id(
            original_task_hash, idx, str(entry.get("description", ""))
        )
        for idx, entry in enumerate(raw_subtasks)
    ]

    result: list[ModelSubtask] = []
    for idx, entry in enumerate(raw_subtasks):
        description = str(entry.get("description", ""))
        model_affinity = str(entry.get("model_affinity", ""))
        if model_affinity and model_affinity not in endpoint_ids:
            model_affinity = ""
        raw_depends = entry.get("depends_on", [])
        if isinstance(raw_depends, list):
            resolved: list[str] = []
            for d in raw_depends:
                try:
                    dep_idx = int(d)
                    if 0 <= dep_idx < len(subtask_ids):
                        resolved.append(subtask_ids[dep_idx])
                except (ValueError, TypeError):
                    resolved.append(str(d))
            depends_on = tuple(resolved)
        else:
            depends_on = ()
        estimated_tokens = int(entry.get("estimated_tokens", 0))
        token_estimation_method = str(
            entry.get("token_estimation_method", "char_ratio")
        )
        raw_category = str(entry.get("category", "general"))
        try:
            category = EnumSubtaskCategory(raw_category)
        except ValueError:
            category = EnumSubtaskCategory.GENERAL
        result.append(
            ModelSubtask(
                subtask_id=subtask_ids[idx],
                description=description,
                model_affinity=model_affinity,
                depends_on=depends_on,
                estimated_tokens=estimated_tokens,
                token_estimation_method=token_estimation_method,
                category=category,
            )
        )
    return result


_WHITE, _GRAY, _BLACK = 0, 1, 2


def _detect_cycle(id_to_subtask: dict[str, ModelSubtask]) -> str | None:
    color: dict[str, int] = dict.fromkeys(id_to_subtask, _WHITE)

    def dfs(node_id: str) -> bool:
        color[node_id] = _GRAY
        for dep in id_to_subtask[node_id].depends_on:
            if dep not in color:
                continue
            if color[dep] == _GRAY:
                return True
            if color[dep] == _WHITE and dfs(dep):
                return True
        color[node_id] = _BLACK
        return False

    for sid in id_to_subtask:
        if color[sid] == _WHITE and dfs(sid):
            return f"dependency cycle detected involving subtask {sid!r}"
    return None


def validate_decomposition(
    subtasks: list[ModelSubtask],
    endpoint_ids: list[str],
    context_window_limit: int = 0,
) -> tuple[list[ModelSubtask], list[str], str | None]:
    warnings: list[str] = []

    for st in subtasks:
        if not st.description.strip():
            return [], [], f"subtask {st.subtask_id!r} has empty description"

    cleaned: list[ModelSubtask] = []
    for st in subtasks:
        if st.model_affinity and endpoint_ids and st.model_affinity not in endpoint_ids:
            warnings.append(
                f"subtask {st.subtask_id!r} has unknown model_affinity {st.model_affinity!r}; cleared"
            )
            st = ModelSubtask(
                subtask_id=st.subtask_id,
                description=st.description,
                model_affinity="",
                depends_on=st.depends_on,
                estimated_tokens=st.estimated_tokens,
                token_estimation_method=st.token_estimation_method,
                category=st.category,
            )
        cleaned.append(st)

    known_ids = {st.subtask_id for st in cleaned}

    for st in cleaned:
        for dep in st.depends_on:
            if dep not in known_ids:
                return (
                    [],
                    warnings,
                    f"subtask {st.subtask_id!r} has dangling dependency {dep!r}",
                )

    id_to_subtask = {st.subtask_id: st for st in cleaned}
    cycle_reason = _detect_cycle(id_to_subtask)
    if cycle_reason:
        return [], warnings, cycle_reason

    if context_window_limit > 0:
        for st in cleaned:
            if st.estimated_tokens > context_window_limit:
                warnings.append(
                    f"subtask {st.subtask_id!r} estimated_tokens {st.estimated_tokens} "
                    f"exceeds context_window_limit {context_window_limit}; dispatcher will handle"
                )

    return cleaned, warnings, None


class HandlerSwarmDecomposer:
    def handle(self, request: ModelSwarmDecomposeRequest) -> ModelSwarmDecomposeResult:
        endpoint_ids = list(request.endpoint_ids)
        original_task = request.original_task
        task_hash = (
            _task_hash(original_task)
            if original_task
            else _task_hash(request.planner_output_hash)
        )
        run_id = str(uuid_mod.uuid4())
        corr = request.correlation_id or str(uuid_mod.uuid4())

        if not request.decompose:
            decomposition = ModelDecomposition(
                original_task=original_task,
                original_task_hash=task_hash,
                subtasks=(_passthrough_subtask(original_task, task_hash),),
                decomposition_model=request.planner_model_id,
                decomposition_endpoint_id=endpoint_ids[0] if endpoint_ids else "",
                decomposition_latency_ms=0,
                decomposition_status=EnumDecompositionStatus.PASSTHROUGH_CALLER_DISABLED,
                decomposition_run_id=run_id,
                correlation_id=corr,
            )
            return ModelSwarmDecomposeResult(
                decomposition=decomposition,
                status=EnumDecompositionStatus.PASSTHROUGH_CALLER_DISABLED,
            )

        if original_task and len(original_task) < request.token_threshold:
            decomposition = ModelDecomposition(
                original_task=original_task,
                original_task_hash=task_hash,
                subtasks=(_passthrough_subtask(original_task, task_hash),),
                decomposition_model=request.planner_model_id,
                decomposition_endpoint_id=endpoint_ids[0] if endpoint_ids else "",
                decomposition_latency_ms=0,
                decomposition_status=EnumDecompositionStatus.PASSTHROUGH_TOKEN_THRESHOLD,
                decomposition_run_id=run_id,
                correlation_id=corr,
            )
            return ModelSwarmDecomposeResult(
                decomposition=decomposition,
                status=EnumDecompositionStatus.PASSTHROUGH_TOKEN_THRESHOLD,
            )

        try:
            subtasks = _parse_subtasks(request.planner_output, task_hash, endpoint_ids)
        except (json.JSONDecodeError, KeyError, TypeError):
            subtasks = []

        if not subtasks:
            decomposition = ModelDecomposition(
                original_task=original_task,
                original_task_hash=task_hash,
                subtasks=(_passthrough_subtask(original_task, task_hash),),
                decomposition_model=request.planner_model_id,
                decomposition_endpoint_id=endpoint_ids[0] if endpoint_ids else "",
                decomposition_latency_ms=0,
                decomposition_status=EnumDecompositionStatus.FAILED_FALLBACK_PASSTHROUGH,
                decomposition_run_id=run_id,
                correlation_id=corr,
            )
            return ModelSwarmDecomposeResult(
                decomposition=decomposition,
                status=EnumDecompositionStatus.FAILED_FALLBACK_PASSTHROUGH,
            )

        cleaned, warns, rejection = validate_decomposition(
            subtasks, endpoint_ids, request.context_window_limit
        )
        if rejection:
            decomposition = ModelDecomposition(
                original_task=original_task,
                original_task_hash=task_hash,
                subtasks=(_passthrough_subtask(original_task, task_hash),),
                decomposition_model=request.planner_model_id,
                decomposition_endpoint_id=endpoint_ids[0] if endpoint_ids else "",
                decomposition_latency_ms=0,
                decomposition_status=EnumDecompositionStatus.FAILED_FALLBACK_PASSTHROUGH,
                decomposition_run_id=run_id,
                correlation_id=corr,
                warnings=(rejection,),
            )
            return ModelSwarmDecomposeResult(
                decomposition=decomposition,
                status=EnumDecompositionStatus.FAILED_FALLBACK_PASSTHROUGH,
                warnings=(rejection,),
            )

        decomposition = ModelDecomposition(
            original_task=original_task,
            original_task_hash=task_hash,
            subtasks=tuple(cleaned),
            decomposition_model=request.planner_model_id,
            decomposition_endpoint_id=endpoint_ids[0] if endpoint_ids else "",
            decomposition_latency_ms=0,
            decomposition_status=EnumDecompositionStatus.SUCCEEDED,
            decomposition_run_id=run_id,
            correlation_id=corr,
            warnings=tuple(warns),
        )
        return ModelSwarmDecomposeResult(
            decomposition=decomposition,
            status=EnumDecompositionStatus.SUCCEEDED,
            warnings=tuple(warns),
        )
