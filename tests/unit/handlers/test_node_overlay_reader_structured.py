# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The structured overlay load: list and mapping fields read through a model (OMN-21373)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)

pytestmark = pytest.mark.unit


class _Roles(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    roles: tuple[str, ...]


def _env(root: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _write(root: Path, text: str | bytes) -> Path:
    path = root / "roles_overlay" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_absent_overlay_reads_as_none(tmp_path: Path) -> None:
    assert load_structured_overlay("roles_overlay", _Roles, _env(tmp_path)) is None


def test_overlay_validates_into_the_model(tmp_path: Path) -> None:
    _write(tmp_path, "roles: [role-a, role-b]\n")
    loaded = load_structured_overlay("roles_overlay", _Roles, _env(tmp_path))
    assert loaded == _Roles(roles=("role-a", "role-b"))


def test_unreadable_overlay_names_the_path_and_the_exception_type(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, b"\xff")
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay("roles_overlay", _Roles, _env(tmp_path))
    assert str(error.value) == (
        f"ONEX_SKILL_OVERLAY_ROOTS: overlay {path} is unreadable (UnicodeDecodeError)"
    )


@pytest.mark.parametrize(
    ("text", "field"),
    [("roles: 7\n", "roles"), ("other: [x]\n", "other"), ("[]\n", "<document>")],
)
def test_model_violation_names_the_path_and_the_field(
    tmp_path: Path, text: str, field: str
) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay("roles_overlay", _Roles, _env(tmp_path))
    assert str(path) in str(error.value)
    assert field in str(error.value)
