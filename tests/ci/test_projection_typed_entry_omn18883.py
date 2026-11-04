# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18883 AC4: the projection typed-entry gate catches trap one.

Trap one: a contract that writes a ``db_io`` table routes a class that reaches
``BaseProjectionRunner`` but overrides ``handle`` with a typed def-B request.
The runtime wires a typed ``handle`` on the typed arm (omnibase_infra
``handler_wiring``, ``_typed_def_b_input_model``), so the
runner's write path is never called and the projection stores zero rows while
its consumer commits every offset. The red control is the pre-fix
``node_projection_lab_lane_health`` shape (OMN-18769 C2, before omnimarket#2700):
its routed ``HandlerProjectionLabLaneHealth(BaseProjectionRunner)`` declared
``handle(self, request: ModelLabLaneHealthRequest) -> ModelLabLaneHealthResult``
and the result carried ``applied`` and ``rows``, no ``rows_upserted``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.models.nodes.no_utcnow_check.model_source_file import ModelSourceFile

from omnimarket.models.contract_projection_check import (
    EnumProjectionContractRule,
    ModelProjectionContractCheckInput,
    ModelProjectionNodeSources,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.findings import (
    RULE_TYPED_ENTRY,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.handler_projection_contract_check import (
    HandlerProjectionContractCheck,
)
from omnimarket.nodes.node_contract_projection_check_effect.handlers.handler_contract_projection_gather import (
    HandlerContractProjectionGather,
)
from omnimarket.nodes.node_contract_projection_check_effect.models import (
    ModelContractProjectionGatherRequest,
)
from tests.ci.projection_contract_check_runners import REPO_ROOT, run_main

pytestmark = [pytest.mark.unit]

_NODE = "node_projection_lab_lane_health"
_HANDLERS = f"src/omnimarket/nodes/{_NODE}/handlers"
_MODELS = f"src/omnimarket/nodes/{_NODE}/models"
_MODULE = f"omnimarket.nodes.{_NODE}.handlers.handler_lab_lane_health_runner"


def _contract(routed: str, access: str = "read_write") -> str:
    return f"""\
name: projection_lab_lane_health
node_type: reducer
handler:
  module: {_MODULE}
  class: HandlerProjectionLabLaneHealth
handler_routing:
  routing_strategy: operation_match
  handlers:
    - operation: lab_lane_health_projection
      handler:
        name: {routed}
        module: {_MODULE}
      handler_module: {_MODULE}
db_io:
  db_tables:
    - name: lab_lane_health
      access: {access}
"""


# The pre-fix runner, reduced to the lines the gate reads: the class line and
# the typed ``handle`` signature are as they stood at e1c4c8f61.
_PRE_FIX_RUNNER = """\
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_request import (
    ModelLabLaneHealthRequest,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_result import (
    ModelLabLaneHealthResult,
)


class HandlerProjectionLabLaneHealth(BaseProjectionRunner):
    def handle(self, request: ModelLabLaneHealthRequest) -> ModelLabLaneHealthResult:
        rows = {}
        touched = apply_event(rows, topic=request.topic, payload=request.payload)
        return ModelLabLaneHealthResult(applied=True, rows=tuple(touched))

    async def project_event(self, topic, data, meta: MessageMeta) -> bool:
        return True
"""

_POST_FIX_WRITER = """\
from omnimarket.projection.runner import BaseProjectionRunner


class HandlerProjectionLabLaneHealth:
    def handle(self, request: ModelLabLaneHealthRequest) -> ModelLabLaneHealthResult:
        return fold(request)


class LabLaneHealthProjectionWriter(BaseProjectionRunner):
    onex_runtime_inprocess_dispatch = True

    async def project_event(self, topic, data, meta) -> bool:
        return True
"""

_RESULT_NO_COUNT = """\
from pydantic import BaseModel


class ModelLabLaneHealthResult(BaseModel):
    applied: bool
    rows: tuple[dict[str, object], ...] = ()
"""

_RESULT_WITH_COUNT = """\
from pydantic import BaseModel


class ModelLabLaneHealthResult(BaseModel):
    applied: bool
    rows_upserted: int = 0
"""


def _node(
    routed: str,
    runner: str,
    result: str = _RESULT_NO_COUNT,
    access: str = "read_write",
) -> ModelProjectionNodeSources:
    return ModelProjectionNodeSources(
        node=_NODE,
        contract_path=f"src/omnimarket/nodes/{_NODE}/contract.yaml",
        contract_text=_contract(routed, access),
        modules=(
            ModelSourceFile(
                path=f"{_HANDLERS}/handler_lab_lane_health_runner.py", source=runner
            ),
            ModelSourceFile(
                path=f"{_MODELS}/model_lab_lane_health_result.py", source=result
            ),
        ),
    )


def _findings(*nodes: ModelProjectionNodeSources) -> list[tuple[str, str]]:
    report = HandlerProjectionContractCheck().handle(
        ModelProjectionContractCheckInput(
            rule=EnumProjectionContractRule.TYPED_ENTRY, nodes=nodes
        )
    )
    return [(f.rule_id, f.message) for f in report.findings]


def test_red_control_the_pre_fix_lab_lane_health_shape_is_refused() -> None:
    findings = _findings(_node("HandlerProjectionLabLaneHealth", _PRE_FIX_RUNNER))
    assert [rule for rule, _ in findings] == [RULE_TYPED_ENTRY]
    message = findings[0][1]
    assert "HandlerProjectionLabLaneHealth" in message
    assert "ModelLabLaneHealthRequest" in message
    assert "handler_lab_lane_health_runner.py:11" in message


def test_the_post_fix_two_class_shape_passes() -> None:
    assert _findings(_node("LabLaneHealthProjectionWriter", _POST_FIX_WRITER)) == []


def test_a_typed_facade_that_reports_rows_upserted_passes() -> None:
    assert (
        _findings(
            _node("HandlerProjectionLabLaneHealth", _PRE_FIX_RUNNER, _RESULT_WITH_COUNT)
        )
        == []
    )


def test_a_read_only_contract_is_out_of_scope() -> None:
    assert (
        _findings(
            _node("HandlerProjectionLabLaneHealth", _PRE_FIX_RUNNER, access="read")
        )
        == []
    )


def test_a_typed_handle_on_an_in_node_intermediate_base_is_refused() -> None:
    runner = """\
from omnimarket.projection.runner import BaseProjectionRunner


class _LaneHealthBase(BaseProjectionRunner):
    def handle(self, request: ModelLabLaneHealthRequest) -> ModelLabLaneHealthResult:
        return fold(request)


class HandlerProjectionLabLaneHealth(_LaneHealthBase):
    pass
"""
    findings = _findings(_node("HandlerProjectionLabLaneHealth", runner))
    assert [rule for rule, _ in findings] == [RULE_TYPED_ENTRY]
    assert "_LaneHealthBase.handle" in findings[0][1]


def test_a_typed_fold_that_does_not_reach_the_runner_base_is_out_of_scope() -> None:
    fold = """\
class HandlerProjectionLabLaneHealth:
    def handle(self, request: ModelLabLaneHealthRequest) -> ModelLabLaneHealthResult:
        return fold(request)
"""
    assert _findings(_node("HandlerProjectionLabLaneHealth", fold)) == []


def test_the_live_tree_passes_and_the_scan_is_not_vacuous() -> None:
    check_input = HandlerContractProjectionGather().handle(
        ModelContractProjectionGatherRequest(
            root=str(REPO_ROOT), rule=EnumProjectionContractRule.TYPED_ENTRY.value
        )
    )
    writing = [
        node
        for node in check_input.nodes
        if any(
            isinstance(t, dict) and t.get("access") in {"write", "read_write"}
            for t in (
                (yaml.safe_load(node.contract_text) or {}).get("db_io") or {}
            ).get("db_tables")
            or []
        )
    ]
    assert len(writing) >= 40, len(writing)
    report = HandlerProjectionContractCheck().handle(check_input)
    assert [f.message for f in report.findings] == []


def _materialize(root: Path, node: ModelProjectionNodeSources) -> None:
    files = {node.contract_path: node.contract_text}
    files.update({m.path: m.source for m in node.modules})
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def test_the_entrypoint_exits_non_zero_on_the_red_control(tmp_path: Path) -> None:
    _materialize(tmp_path, _node("HandlerProjectionLabLaneHealth", _PRE_FIX_RUNNER))
    rc, _out, err = run_main(tmp_path, ["--rule", "typed-entry"])
    assert rc == 1
    assert "Projection typed-entry gate FAILED" in err


def test_the_entrypoint_exits_zero_on_the_post_fix_shape(tmp_path: Path) -> None:
    _materialize(tmp_path, _node("LabLaneHealthProjectionWriter", _POST_FIX_WRITER))
    rc, out, _err = run_main(tmp_path, ["--rule", "typed-entry"])
    assert rc == 0
    assert "Projection typed-entry gate OK" in out


def test_the_gate_is_wired_into_ci_and_pre_commit() -> None:
    command = "runtime_projection_contract_check --rule typed-entry"
    precommit = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    hooks = [
        hook
        for repo in precommit["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "projection-typed-entry"
    ]
    assert len(hooks) == 1
    assert command in hooks[0]["entry"]
    assert hooks[0].get("stages") == ["pre-commit"]
    ci = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    lint_steps = ci["jobs"]["lint"]["steps"]
    assert any(command in str(step.get("run", "")) for step in lint_steps)
