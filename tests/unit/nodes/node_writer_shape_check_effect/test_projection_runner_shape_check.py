# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18883: the projection runner shape check refuses both silent-failure shapes.

Every refusal is proved RED over a synthetic tree built in ``tmp_path`` and the real
tree is proved clean, so a pass is never a scan that saw nothing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_writer_shape_check_effect.handlers.handler_projection_runner_shape_check import (
    RULE_GROWTH,
    RULE_STALE,
    RULE_TYPED_FOLD,
    RULE_UNDECLARED,
    RULE_VACUOUS,
    HandlerProjectionRunnerShapeCheck,
)
from omnimarket.nodes.node_writer_shape_check_effect.handlers.projection_runner_frozen_set import (
    DEDICATED_DEPLOYMENT_RUNNERS,
    FROZEN_RUNNERS,
    MIN_RUNNERS,
)
from omnimarket.nodes.node_writer_shape_check_effect.models import (
    ModelProjectionRunnerShapeRequest,
    ModelProjectionRunnerShapeResult,
)

REPO = Path(__file__).resolve().parents[4]
_MODULE_PREFIX = "omnimarket.nodes.node_projection_probe"


def _write_node(
    root: Path,
    *,
    handler_source: str,
    class_name: str,
    node_type: str = "REDUCER_GENERIC",
    db_tables: bool = True,
) -> None:
    node = root / "src" / "omnimarket" / "nodes" / "node_projection_probe"
    (node / "handlers").mkdir(parents=True)
    module = f"{_MODULE_PREFIX}.handlers.handler_probe"
    contract: dict[str, object] = {
        "name": "node_projection_probe",
        "node_type": node_type,
        "handler_routing": {
            "handlers": [
                {"handler": {"name": class_name, "module": module}},
            ]
        },
    }
    if db_tables:
        contract["db_io"] = {"db_tables": [{"name": "probe_rows"}]}
    (node / "contract.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
    (node / "handlers" / "handler_probe.py").write_text(
        textwrap.dedent(handler_source), encoding="utf-8"
    )


def _run(
    root: Path,
    *,
    frozen: tuple[str, ...] = (),
    dedicated: tuple[str, ...] = (),
    min_runners: int = 0,
) -> ModelProjectionRunnerShapeResult:
    return HandlerProjectionRunnerShapeCheck().handle(
        ModelProjectionRunnerShapeRequest(
            nodes_root=str(root / "src" / "omnimarket" / "nodes"),
            package_root=str(root / "src"),
            frozen_runners=frozen,
            dedicated_deployments=dedicated,
            min_runners=min_runners,
        )
    )


def _rules(result: ModelProjectionRunnerShapeResult) -> list[str]:
    return [f.rule for f in result.findings]


# The shape node_projection_lab_lane_health had before it was fixed: the fold is
# routed alone and takes a typed request, so the wiring path's injected dict
# validates and returns without a write.
_TYPED_FOLD = """
    class ModelLaneHealth:
        pass

    class HandlerProjectionLabLaneHealth:
        def handle(self, request: ModelLaneHealth) -> ModelLaneHealth:
            return request
"""

_DICT_FOLD = """
    class HandlerProjectionLabLaneHealth:
        def handle(self, input_data: dict[str, object]) -> dict[str, object]:
            return input_data
"""

_INPROCESS_RUNNER = """
    class BaseProjectionRunner:
        pass

    class ProbeProjectionWriter(BaseProjectionRunner):
        onex_runtime_inprocess_dispatch = True
"""

_STANDALONE_RUNNER = """
    class BaseProjectionRunner:
        pass

    class ProbeProjectionRunner(BaseProjectionRunner):
        pass
"""


def test_typed_fold_on_writer_path_is_refused(tmp_path: Path) -> None:
    _write_node(
        tmp_path,
        handler_source=_TYPED_FOLD,
        class_name="HandlerProjectionLabLaneHealth",
    )
    result = _run(tmp_path)
    assert _rules(result) == [RULE_TYPED_FOLD]
    assert result.findings[0].subject == (
        f"{_MODULE_PREFIX}.handlers.handler_probe:HandlerProjectionLabLaneHealth"
    )


_INPROCESS_RUNNER_TYPED_HANDLE = """
    class BaseProjectionRunner:
        pass

    class ModelLaneHealth:
        pass

    class HandlerProjectionLabLaneHealth(BaseProjectionRunner):
        onex_runtime_inprocess_dispatch = True

        def handle(self, request: ModelLaneHealth) -> ModelLaneHealth:
            return request
"""

# The pre-fix node_projection_lab_lane_health: a runner subclass with a typed
# handle and no in-process declaration (verified over the real pre-fix tree).
_PRE_FIX_LAB_LANE_HEALTH = _INPROCESS_RUNNER_TYPED_HANDLE.replace(
    "        onex_runtime_inprocess_dispatch = True\n\n", ""
)


def test_inprocess_runner_with_typed_handle_is_refused(tmp_path: Path) -> None:
    _write_node(
        tmp_path,
        handler_source=_INPROCESS_RUNNER_TYPED_HANDLE,
        class_name="HandlerProjectionLabLaneHealth",
    )
    key = f"{_MODULE_PREFIX}.handlers.handler_probe:HandlerProjectionLabLaneHealth"
    assert _rules(_run(tmp_path, frozen=(key,))) == [RULE_TYPED_FOLD]


def test_pre_fix_lab_lane_health_shape_is_refused(tmp_path: Path) -> None:
    _write_node(
        tmp_path,
        handler_source=_PRE_FIX_LAB_LANE_HEALTH,
        class_name="HandlerProjectionLabLaneHealth",
    )
    key = f"{_MODULE_PREFIX}.handlers.handler_probe:HandlerProjectionLabLaneHealth"
    assert "onex_runtime_inprocess_dispatch" not in _PRE_FIX_LAB_LANE_HEALTH
    assert _rules(_run(tmp_path, frozen=(key,))) == [RULE_UNDECLARED]


def test_dict_fold_is_not_the_typed_trap(tmp_path: Path) -> None:
    _write_node(
        tmp_path, handler_source=_DICT_FOLD, class_name="HandlerProjectionLabLaneHealth"
    )
    assert _run(tmp_path).findings == ()


def test_typed_handler_without_db_tables_or_as_effect_is_out_of_scope(
    tmp_path: Path,
) -> None:
    _write_node(
        tmp_path,
        handler_source=_TYPED_FOLD,
        class_name="HandlerProjectionLabLaneHealth",
        db_tables=False,
    )
    assert _run(tmp_path).findings == ()


def test_typed_effect_node_is_out_of_scope(tmp_path: Path) -> None:
    _write_node(
        tmp_path,
        handler_source=_TYPED_FOLD,
        class_name="HandlerProjectionLabLaneHealth",
        node_type="EFFECT_GENERIC",
    )
    assert _run(tmp_path).findings == ()


def test_listed_inprocess_runner_is_clean(tmp_path: Path) -> None:
    _write_node(
        tmp_path, handler_source=_INPROCESS_RUNNER, class_name="ProbeProjectionWriter"
    )
    result = _run(
        tmp_path,
        frozen=(f"{_MODULE_PREFIX}.handlers.handler_probe:ProbeProjectionWriter",),
    )
    assert result.findings == ()
    assert result.runners_seen == 1


def test_runner_without_dispatch_owner_is_refused(tmp_path: Path) -> None:
    _write_node(
        tmp_path, handler_source=_STANDALONE_RUNNER, class_name="ProbeProjectionRunner"
    )
    key = f"{_MODULE_PREFIX}.handlers.handler_probe:ProbeProjectionRunner"
    refused = _run(tmp_path, frozen=(key,))
    assert _rules(refused) == [RULE_UNDECLARED]
    owned = _run(tmp_path, frozen=(key,), dedicated=(key,))
    assert owned.findings == ()


def test_unlisted_runner_subclass_is_growth(tmp_path: Path) -> None:
    _write_node(
        tmp_path, handler_source=_INPROCESS_RUNNER, class_name="ProbeProjectionWriter"
    )
    result = _run(tmp_path)
    assert _rules(result) == [RULE_GROWTH]


def test_entry_for_a_gone_class_is_stale(tmp_path: Path) -> None:
    _write_node(
        tmp_path, handler_source=_DICT_FOLD, class_name="HandlerProjectionLabLaneHealth"
    )
    result = _run(tmp_path, frozen=("omnimarket.nodes.gone.handlers.h:Gone",))
    assert _rules(result) == [RULE_STALE]


def test_scan_below_the_floor_is_refused(tmp_path: Path) -> None:
    _write_node(
        tmp_path, handler_source=_DICT_FOLD, class_name="HandlerProjectionLabLaneHealth"
    )
    assert _rules(_run(tmp_path, min_runners=5)) == [RULE_VACUOUS]


def _real_request(
    frozen: tuple[str, ...] = FROZEN_RUNNERS,
    dedicated: tuple[str, ...] = DEDICATED_DEPLOYMENT_RUNNERS,
) -> ModelProjectionRunnerShapeRequest:
    return ModelProjectionRunnerShapeRequest(
        nodes_root=str(REPO / "src" / "omnimarket" / "nodes"),
        package_root=str(REPO / "src"),
        frozen_runners=frozen,
        dedicated_deployments=dedicated,
        min_runners=MIN_RUNNERS,
    )


def test_real_tree_is_clean_and_not_vacuous() -> None:
    result = HandlerProjectionRunnerShapeCheck().handle(_real_request())
    assert result.findings == ()
    assert result.runners_seen >= MIN_RUNNERS
    assert set(FROZEN_RUNNERS) >= set(DEDICATED_DEPLOYMENT_RUNNERS)


def test_real_tree_red_control_one_unlisted_subclass_fails() -> None:
    """Positive control: removing one frozen entry is the same as adding one subclass."""
    dropped = FROZEN_RUNNERS[0]
    result = HandlerProjectionRunnerShapeCheck().handle(
        _real_request(frozen=FROZEN_RUNNERS[1:])
    )
    assert [(f.rule, f.subject) for f in result.findings] == [(RULE_GROWTH, dropped)]


def test_real_tree_red_control_one_undeclared_runner_fails() -> None:
    dropped = DEDICATED_DEPLOYMENT_RUNNERS[0]
    result = HandlerProjectionRunnerShapeCheck().handle(
        _real_request(dedicated=DEDICATED_DEPLOYMENT_RUNNERS[1:])
    )
    assert [(f.rule, f.subject) for f in result.findings] == [
        (RULE_UNDECLARED, dropped)
    ]


def test_frozen_sets_are_sorted_and_unique() -> None:
    for entries in (FROZEN_RUNNERS, DEDICATED_DEPLOYMENT_RUNNERS):
        assert list(entries) == sorted(set(entries))


GATE = "runtime_writer_shape_check"


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        (".pre-commit-config.yaml", GATE),
        (".github/workflows/ci.yml", GATE),
    ],
)
def test_check_is_wired_into_precommit_and_ci(path: str, needle: str) -> None:
    assert needle in (REPO / path).read_text(encoding="utf-8")


def test_contract_names_the_handler_and_terminal_event() -> None:
    contract = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_writer_shape_check_effect/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["handler"]["class"] == "HandlerProjectionRunnerShapeCheck"
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.projection-runner-shape-checked.v1"
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.projection-runner-shape-check-requested.v1"
    ]
