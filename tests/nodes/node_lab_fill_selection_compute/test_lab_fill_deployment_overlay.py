# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The selection's deployment is read from its overlay file; the delegation epics come from there (OMN-23312)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_fill_selection_compute.handlers.handler_lab_fill_deployment_overlay import (
    OVERLAY_ID,
    load_lab_fill_deployment,
)

pytestmark = pytest.mark.unit

ROLES = (
    "companion_repositories: [change_control]\n"
    "not_work_repositories: [registry]\n"
    "document_repositories: [docs]\n"
)


def _root(tmp_path: Path, text: str) -> dict[str, str]:
    overlay = tmp_path / OVERLAY_ID
    overlay.mkdir()
    (overlay / "overlay.yaml").write_text(text, encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_golden_overlay_declares_the_delegation_epics(tmp_path: Path) -> None:
    env = _root(tmp_path, ROLES + "delegation_epics: [OMN-1, OMN-2]\n")
    deployment = load_lab_fill_deployment(env)
    assert deployment.delegation_epics == ("OMN-1", "OMN-2")
    assert deployment.delegation_owners == ()
    assert deployment.companion_repositories == ("change_control",)


def test_overlay_without_epics_names_no_delegation_work(tmp_path: Path) -> None:
    deployment = load_lab_fill_deployment(_root(tmp_path, ROLES))
    assert deployment.delegation_epics == ()


def test_error_overlay_with_an_unknown_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError):
        load_lab_fill_deployment(_root(tmp_path, ROLES + "delegation_epic: [OMN-1]\n"))


def test_error_missing_overlay_refuses_by_name(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match=OVERLAY_ID):
        load_lab_fill_deployment({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
