# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The structured overlay load: list and mapping fields read through a model (OMN-21373)."""

from __future__ import annotations

import os
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


def test_structured_overlay_refusal_absent_names_id_and_fields(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay("roles_overlay", _Roles, _env(tmp_path))
    assert "roles_overlay" in str(error.value)
    assert "roles" in str(error.value)
    assert "ONEX_SKILL_OVERLAY_ROOTS" in str(error.value)


def test_structured_overlay_precedence_overlay_validates_into_the_model(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "roles: [role-a, role-b]\n")
    loaded = load_structured_overlay("roles_overlay", _Roles, _env(tmp_path))
    assert loaded == _Roles(roles=("role-a", "role-b"))


def test_structured_overlay_refusal_unreadable_names_path_and_exception_type(
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
def test_structured_overlay_refusal_model_violation_names_path_and_field(
    tmp_path: Path, text: str, field: str
) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay("roles_overlay", _Roles, _env(tmp_path))
    assert str(path) in str(error.value)
    assert field in str(error.value)


class _Host(BaseModel):
    port: int


class _Registry(BaseModel):
    hosts: list[_Host]
    roles: dict[str, list[str]]


def test_structured_overlay_precedence_explicit_model_wins(tmp_path: Path) -> None:
    _write(tmp_path, "roles: 7\n")
    env = {**_env(tmp_path), "TEST_OVERLAY_FILE": str(tmp_path / "missing.yaml")}
    explicit = _Roles(roles=("explicit-role",))
    loaded = load_structured_overlay(
        "roles_overlay", _Roles, env, explicit=explicit, file_env="TEST_OVERLAY_FILE"
    )
    assert loaded is explicit


def test_structured_overlay_precedence_explicit_data_is_validated(
    tmp_path: Path,
) -> None:
    loaded = load_structured_overlay(
        "roles_overlay", _Roles, _env(tmp_path), explicit={"roles": ["explicit-role"]}
    )
    assert loaded == _Roles(roles=("explicit-role",))


def test_structured_overlay_precedence_pinned_file_wins(tmp_path: Path) -> None:
    _write(tmp_path, "roles: 7\n")
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("roles: [file-role]\n", encoding="utf-8")
    env = {**_env(tmp_path), "TEST_OVERLAY_FILE": str(pinned)}
    assert load_structured_overlay(
        "roles_overlay", _Roles, env, file_env="TEST_OVERLAY_FILE"
    ) == _Roles(roles=("file-role",))


@pytest.mark.parametrize("pinned", [None, "", "   "])
def test_structured_overlay_precedence_unset_file_env_uses_first_root(
    tmp_path: Path, pinned: str | None
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write(first, "roles: [first-role]\n")
    _write(second, "roles: [second-role]\n")
    env = {
        "ONEX_SKILL_OVERLAY_ROOTS": os.pathsep.join(
            (str(tmp_path / "absent"), str(first), str(second))
        )
    }
    if pinned is not None:
        env["TEST_OVERLAY_FILE"] = pinned
    assert load_structured_overlay(
        "roles_overlay", _Roles, env, file_env="TEST_OVERLAY_FILE"
    ) == _Roles(roles=("first-role",))


def test_structured_overlay_precedence_lists_and_mappings(tmp_path: Path) -> None:
    _write(tmp_path, "hosts: [{port: 8080}]\nroles: {reader: [role-a]}\n")
    loaded = load_structured_overlay("roles_overlay", _Registry, _env(tmp_path))
    assert loaded.hosts == [_Host(port=8080)]
    assert loaded.roles == {"reader": ["role-a"]}


@pytest.mark.parametrize(
    ("content", "exception_type"),
    [
        (None, "FileNotFoundError"),
        (b"\xff", "UnicodeDecodeError"),
        ("roles: [", "ParserError"),
        ("roles: 7\n", "ValidationError"),
    ],
)
def test_structured_overlay_refusal_pinned_file_does_not_fall_back(
    tmp_path: Path, content: str | bytes | None, exception_type: str
) -> None:
    _write(tmp_path, "roles: [fallback-role]\n")
    pinned = tmp_path / "pinned.yaml"
    if isinstance(content, bytes):
        pinned.write_bytes(content)
    elif content is not None:
        pinned.write_text(content, encoding="utf-8")
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay(
            "roles_overlay",
            _Roles,
            {**_env(tmp_path), "TEST_OVERLAY_FILE": str(pinned)},
            file_env="TEST_OVERLAY_FILE",
        )
    assert str(pinned) in str(error.value)
    assert exception_type in str(error.value)


def test_structured_overlay_refusal_invalid_explicit_does_not_fall_back(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "roles: [fallback-role]\n")
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay(
            "roles_overlay", _Roles, _env(tmp_path), explicit={"roles": 7}
        )
    assert "roles" in str(error.value)
    assert "ValidationError" in str(error.value)


def test_structured_overlay_refusal_nested_field_redacts_private_values(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "hosts: [{port: private-marker}]\nroles: {}\n")
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay("roles_overlay", _Registry, _env(tmp_path))
    assert "hosts.0.port" in str(error.value)
    assert "private-marker" not in str(error.value)
    assert error.value.__suppress_context__


def test_structured_overlay_refusal_broken_first_root_does_not_fall_back(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    path = _write(first, "roles: [")
    _write(second, "roles: [fallback-role]\n")
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay(
            "roles_overlay",
            _Roles,
            {"ONEX_SKILL_OVERLAY_ROOTS": os.pathsep.join((str(first), str(second)))},
        )
    assert str(path) in str(error.value)
    assert "ParserError" in str(error.value)


def test_structured_overlay_precedence_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "roles: [overlay-role]\n")
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("roles: [file-role]\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("TEST_OVERLAY_FILE", str(pinned))
    assert load_structured_overlay(
        "roles_overlay", _Roles, file_env="TEST_OVERLAY_FILE"
    ) == _Roles(roles=("file-role",))
    monkeypatch.delenv("TEST_OVERLAY_FILE")
    assert load_structured_overlay(
        "roles_overlay", _Roles, file_env="TEST_OVERLAY_FILE"
    ) == _Roles(roles=("overlay-role",))


def test_structured_overlay_refusal_empty_explicit_is_not_absent(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "roles: [fallback-role]\n")
    with pytest.raises(NodeOverlayError, match="roles"):
        load_structured_overlay("roles_overlay", _Roles, _env(tmp_path), explicit={})


def test_structured_overlay_refusal_missing_names_file_env(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError) as error:
        load_structured_overlay(
            "roles_overlay", _Roles, _env(tmp_path), file_env="TEST_OVERLAY_FILE"
        )
    assert "TEST_OVERLAY_FILE" in str(error.value)
    assert "<root>/roles_overlay/overlay.yaml" in str(error.value)
