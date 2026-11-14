# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The history effect's contract and overlay: nothing deployment-specific is packaged, and a name that could break the shell is refused.

Golden chain: the contract's runtime block and the fixture overlay load into one runtime. Error chain: no overlay refuses by name with no
packaged fallback, a database name with a quote or a space is refused before any command is built, and a report pattern with a shell
metacharacter is refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.handler_floor_delegation_history import (
    OWN_CONTRACT,
    load_runtime,
)
from omnimarket.nodes.node_floor_delegation_history_effect.models.model_floor_delegation_history import (
    ModelHistoryOverlay,
)

from .support import FIXTURES, OVERLAY, RT

pytestmark = pytest.mark.unit


def _document() -> dict:
    return yaml.safe_load((FIXTURES / "overlay.yaml").read_text(encoding="utf-8"))


def test_golden_the_contract_and_the_overlay_make_one_runtime() -> None:
    assert RT.output_relpath == ".claude_scratch/factory-floor/delegation-history.json"
    assert RT.database.compose_project == OVERLAY.database.compose_project
    assert RT.policy.window_days == 14
    contract = yaml.safe_load(OWN_CONTRACT.read_text(encoding="utf-8"))
    assert contract["node_type"] == "effect"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}


def test_error_no_overlay_refuses_by_name_with_no_packaged_fallback(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match="node_floor_delegation_history_effect"):
        load_runtime(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


@pytest.mark.parametrize(
    "bad", ["omnidash analytics", "db'; DROP TABLE x; --", "$(id)"]
)
def test_error_a_database_name_that_could_break_the_command_is_refused(
    bad: str,
) -> None:
    doc = _document()
    doc["database"]["database"] = bad
    with pytest.raises(ValidationError):
        ModelHistoryOverlay.model_validate(doc)


def test_error_a_report_pattern_with_a_shell_metacharacter_is_refused() -> None:
    doc = _document()
    doc["reports"]["sources"][0]["root"] = "/registry/reports; rm -rf /"
    with pytest.raises(ValidationError):
        ModelHistoryOverlay.model_validate(doc)


def test_error_an_overlay_with_an_unknown_key_is_refused() -> None:
    doc = _document()
    doc["policy"]["invented"] = 1
    with pytest.raises(ValidationError):
        ModelHistoryOverlay.model_validate(doc)
