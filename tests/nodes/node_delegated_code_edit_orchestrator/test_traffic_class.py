# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The code edit request names the origin of its run (OMN-23870).

``traffic_class`` is the core delegation enum, ``unclassified`` when the caller
names none, and a synthetic class without a ``source_surface`` is refused when
the request is built, before the loop has started.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from pydantic import ValidationError

from omnimarket.cli.cli_code_edit import run_command
from omnimarket.handlers.code_edit_request_builder import build_code_edit_request
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    _request,
)

pytestmark = pytest.mark.unit


def test_traffic_class_defaults_to_unclassified_never_organic() -> None:
    request = _request()
    assert request.traffic_class is EnumDelegationTrafficClass.UNCLASSIFIED
    assert request.source_surface is None


def test_traffic_class_of_an_old_request_file_without_the_field_reads_unclassified() -> (
    None
):
    payload = _request().model_dump(mode="json", exclude={"traffic_class"})
    assert "traffic_class" not in payload
    restored = ModelDelegatedCodeEditRequest.model_validate(payload)
    assert restored.traffic_class is EnumDelegationTrafficClass.UNCLASSIFIED


def test_traffic_class_synthetic_without_a_source_surface_is_refused() -> None:
    with pytest.raises(ValidationError, match="synthetic traffic requires"):
        _request(traffic_class=EnumDelegationTrafficClass.SYNTHETIC)


def test_traffic_class_synthetic_with_a_source_surface_is_accepted() -> None:
    request = _request(
        traffic_class=EnumDelegationTrafficClass.SYNTHETIC, source_surface="trial"
    )
    assert request.traffic_class is EnumDelegationTrafficClass.SYNTHETIC
    assert request.source_surface == "trial"


def test_traffic_class_organic_needs_no_source_surface() -> None:
    request = _request(traffic_class=EnumDelegationTrafficClass.ORGANIC)
    assert request.traffic_class is EnumDelegationTrafficClass.ORGANIC


def test_traffic_class_is_a_core_enum_value_not_free_text() -> None:
    with pytest.raises(ValidationError):
        _request(traffic_class="trial")


def test_traffic_class_builder_refuses_synthetic_without_surface(
    tmp_path: object,
) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    task = root / "task.txt"
    task.write_text("do it")
    common = {
        "worktree": root,
        "task_file": task,
        "writable": ("src/*.py",),
        "context_paths": (),
        "file_list": (),
        "cli_checks": (ModelDeclaredCheck(name="t", argv=("true",)),),
        "formatter": (),
        "max_turns": 2,
        "check_timeout_s": 10,
        "wall_budget_s": None,
        "ticket_contract": None,
        "caller_lane": "lane-x",
        "ticket": "OMN-23870",
    }
    with pytest.raises(ValueError, match="synthetic traffic requires"):
        build_code_edit_request(
            **common,  # type: ignore[arg-type]
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
        )
    built = build_code_edit_request(
        **common,  # type: ignore[arg-type]
        traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
        source_surface="trial",
    )
    assert built.traffic_class is EnumDelegationTrafficClass.SYNTHETIC
    assert built.source_surface == "trial"
    default = build_code_edit_request(**common)  # type: ignore[arg-type]
    assert default.traffic_class is EnumDelegationTrafficClass.UNCLASSIFIED


def test_traffic_class_cli_refuses_synthetic_without_surface_before_any_turn(
    tmp_path: object,
) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    task = root / "task.txt"
    task.write_text("do it")
    result = CliRunner().invoke(
        run_command,
        [
            "--worktree",
            str(root),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--check",
            "t=true",
            "--traffic-class",
            "synthetic",
        ],
    )
    assert result.exit_code != 0
    assert "synthetic traffic requires a source_surface" in result.output
