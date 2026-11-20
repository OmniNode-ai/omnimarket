# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the delegation step order: its model and its loader (OMN-24507).

The overlay is present but invalid, never a default: a document that names an engine twice, leaves an engine without a
program or carries an engine word or key outside the model refuses, and the loader's file reader refuses the same way
with :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.delegation_step_order_overlay import (
    load_delegation_step_order_overlay,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.model_delegation_step_order_overlay import (
    ModelDelegationStepOrderOverlay,
)

pytestmark = pytest.mark.unit


def _golden() -> dict[str, object]:
    return {
        "code_engine_order": ["lab", "glm"],
        "engine_programs": {"lab": None, "glm": "claude"},
    }


def _write_overlay(root: Path, document: str) -> None:
    """Write the deployment's delegation step order overlay under ``root``."""
    step_dir = root / "delegation_step_order"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "overlay.yaml").write_text(document, encoding="utf-8")


def _environ(root: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_a_golden_overlay_validates() -> None:
    overlay = ModelDelegationStepOrderOverlay.model_validate(_golden())
    assert overlay.code_engine_order == ("lab", "glm")
    assert overlay.engine_programs == {"lab": None, "glm": "claude"}


def test_an_order_that_names_an_engine_twice_is_refused() -> None:
    document = _golden()
    document["code_engine_order"] = ["lab", "glm", "lab"]
    with pytest.raises(ValidationError):
        ModelDelegationStepOrderOverlay.model_validate(document)


def test_engine_programs_missing_an_engine_is_refused() -> None:
    document = _golden()
    document["engine_programs"] = {"lab": None}
    with pytest.raises(ValidationError):
        ModelDelegationStepOrderOverlay.model_validate(document)


@pytest.mark.parametrize("where", ["code_engine_order", "engine_programs"])
def test_an_unknown_engine_word_is_refused(where: str) -> None:
    document = _golden()
    if where == "code_engine_order":
        document["code_engine_order"] = ["lab", "codex"]
    else:
        document["engine_programs"] = {"lab": None, "glm": None, "codex": "claude"}
    with pytest.raises(ValidationError):
        ModelDelegationStepOrderOverlay.model_validate(document)


def test_an_extra_key_is_refused() -> None:
    document = _golden()
    document["stray"] = 1
    with pytest.raises(ValidationError):
        ModelDelegationStepOrderOverlay.model_validate(document)


def test_the_loader_answers_none_with_no_overlay_file(tmp_path: Path) -> None:
    assert load_delegation_step_order_overlay(_environ(tmp_path)) is None


def test_the_loader_reads_a_valid_overlay_file(tmp_path: Path) -> None:
    _write_overlay(
        tmp_path,
        "code_engine_order: [lab, glm]\nengine_programs: {lab: null, glm: sh}\n",
    )
    assert load_delegation_step_order_overlay(_environ(tmp_path)) == (
        ModelDelegationStepOrderOverlay(
            code_engine_order=("lab", "glm"), engine_programs={"lab": None, "glm": "sh"}
        )
    )


def test_the_loader_refuses_an_invalid_overlay_file(tmp_path: Path) -> None:
    _write_overlay(
        tmp_path,
        "code_engine_order: []\nengine_programs: {lab: null, glm: sh}\n",
    )
    with pytest.raises(NodeOverlayError) as caught:
        load_delegation_step_order_overlay(_environ(tmp_path))
    assert not isinstance(caught.value, NodeOverlayNotFoundError)
