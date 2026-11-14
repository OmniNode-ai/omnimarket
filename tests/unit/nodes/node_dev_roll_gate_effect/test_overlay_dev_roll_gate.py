# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every value of node_dev_roll_gate_effect comes from an overlay (OMN-24431 AC1).

No packaged fallback, no default on any policy field, and no number written in the node's
code: the window and the minimum start interval are the batching node's overlay values.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.handler_dev_roll_gate_mode import dev_roll_gate_is_live
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_dev_roll_gate_effect import overlay as gate_overlay
from omnimarket.nodes.node_dev_roll_gate_effect.models import ModelDevRollGatePolicy
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import GATE_DOC

pytestmark = pytest.mark.unit

NODE_DIR = Path(gate_overlay.__file__).parent
SEEDED = {0, 1, -1}


def _write(root: Path, doc: object) -> dict[str, str]:
    node = root / gate_overlay.NODE_NAME
    node.mkdir(parents=True, exist_ok=True)
    (node / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_the_policy_reads_from_the_overlay(tmp_path: Path) -> None:
    policy = gate_overlay.load_gate_policy(_write(tmp_path, GATE_DOC))
    assert policy.mode == "live"
    assert (
        policy.roll_unfinished_after_seconds
        == GATE_DOC["roll_unfinished_after_seconds"]
    )


def test_no_overlay_is_a_refusal_not_a_packaged_default(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayNotFoundError):
        gate_overlay.load_gate_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


@pytest.mark.parametrize("field", sorted(GATE_DOC))
def test_a_missing_field_is_refused(tmp_path: Path, field: str) -> None:
    doc = {k: v for k, v in GATE_DOC.items() if k != field}
    with pytest.raises(NodeOverlayError):
        gate_overlay.load_gate_policy(_write(tmp_path, doc))


def test_no_policy_field_has_a_default() -> None:
    assert all(f.is_required() for f in ModelDevRollGatePolicy.model_fields.values())


def test_a_pattern_with_no_repository_group_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError):
        gate_overlay.load_gate_policy(
            _write(tmp_path, {**GATE_DOC, "issuer_repository_pattern": "^gha/.*$"})
        )


def test_the_example_overlay_is_a_valid_document() -> None:
    doc = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert ModelDevRollGatePolicy.model_validate(doc).mode == "live"


def _numbers(source: str) -> set[object]:
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    } - SEEDED


def test_no_number_is_written_in_the_nodes_code() -> None:
    offenders = {
        str(path.relative_to(NODE_DIR)): _numbers(path.read_text(encoding="utf-8"))
        for path in NODE_DIR.rglob("*.py")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_failing_control_a_window_written_in_code_is_found() -> None:
    assert _numbers("WINDOW = 600\n") == {600}


# --- the mode the deploy effect reads -------------------------------------------------------------------------------


def test_mode_reads_live_only_from_a_valid_live_overlay(tmp_path: Path) -> None:
    assert dev_roll_gate_is_live(_write(tmp_path, GATE_DOC)) is True


def test_mode_off_and_an_absent_overlay_read_as_not_live(tmp_path: Path) -> None:
    assert dev_roll_gate_is_live(_write(tmp_path, {**GATE_DOC, "mode": "off"})) is False
    assert (
        dev_roll_gate_is_live({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")})
        is False
    )
    assert dev_roll_gate_is_live({}) is False


def test_a_broken_mode_overlay_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError):
        dev_roll_gate_is_live(_write(tmp_path, {"mode": "maybe"}))
