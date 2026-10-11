# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node deployment overlay reader (OMN-20935): found, validated, never defaulted."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_node_overlay,
    not_configured_message,
    resolve_overlay_file,
    resolve_setting,
)

FIELDS = frozenset({"runtime_host"})
NODE = "node_example"


def _write(root: Path, node: str, text: str) -> Path:
    path = root / node / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.unit
def test_first_root_holding_the_overlay_wins(tmp_path: Path) -> None:
    first, second, empty = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    empty.mkdir()
    _write(second, NODE, "runtime_host: second.example.test\n")
    win = _write(first, NODE, "runtime_host: first.example.test\n")
    roots = f"{empty}:{first}:{second}"
    assert find_node_overlay(NODE, {"ONEX_SKILL_OVERLAY_ROOTS": roots}) == win


@pytest.mark.unit
def test_no_root_means_no_overlay_and_an_empty_mapping() -> None:
    assert find_node_overlay(NODE, {}) is None
    assert load_node_overlay(NODE, fields=FIELDS, environ={}) == {}


@pytest.mark.unit
def test_overlay_values_are_stripped_strings(tmp_path: Path) -> None:
    _write(tmp_path, NODE, "runtime_host: ' host.example.test '\n")
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    assert load_node_overlay(NODE, fields=FIELDS, environ=env) == {
        "runtime_host": "host.example.test"
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("- not\n- a mapping\n", "must be a mapping"),
        ("runtime_host: ok\nextra: nope\n", "unknown fields ['extra']"),
        ("runtime_host: 7\n", "must be non-empty strings"),
        ("runtime_host: ''\n", "must be non-empty strings"),
        ("runtime_host: [unterminated\n", "unreadable"),
    ],
)
def test_a_broken_overlay_is_an_error_not_an_absent_one(
    tmp_path: Path, text: str, needle: str
) -> None:
    _write(tmp_path, NODE, text)
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    with pytest.raises(NodeOverlayError, match=re.escape(needle)):
        load_node_overlay(NODE, fields=FIELDS, environ=env)


@pytest.mark.unit
def test_resolve_setting_prefers_the_variables_in_order_then_the_overlay(
    tmp_path: Path,
) -> None:
    _write(tmp_path, NODE, "runtime_host: overlay.example.test\n")
    base = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    kwargs = {"fields": FIELDS, "env_vars": ("FIRST_HOST", "SECOND_HOST")}
    assert (
        resolve_setting(NODE, "runtime_host", environ=base, **kwargs)
        == "overlay.example.test"
    )
    both = {**base, "FIRST_HOST": "first.example.test", "SECOND_HOST": "second.test"}
    assert (
        resolve_setting(NODE, "runtime_host", environ=both, **kwargs)
        == "first.example.test"
    )
    only_second = {**base, "FIRST_HOST": "  ", "SECOND_HOST": "second.example.test"}
    assert (
        resolve_setting(NODE, "runtime_host", environ=only_second, **kwargs)
        == "second.example.test"
    )


@pytest.mark.unit
def test_unsupplied_setting_refuses_and_names_how_to_supply_it() -> None:
    with pytest.raises(NodeOverlayError) as excinfo:
        resolve_setting(
            NODE,
            "runtime_host",
            fields=FIELDS,
            env_vars=("EXAMPLE_HOST",),
            environ={},
        )
    message = str(excinfo.value)
    assert message == not_configured_message(NODE, "runtime_host", ("EXAMPLE_HOST",))
    assert "set EXAMPLE_HOST" in message
    assert f"<root>/{NODE}/overlay.yaml" in message
    assert "ONEX_SKILL_OVERLAY_ROOTS" in message


@pytest.mark.unit
def test_overlay_without_the_requested_field_refuses(tmp_path: Path) -> None:
    _write(tmp_path, NODE, "{}\n")
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    with pytest.raises(NodeOverlayError, match="not configured"):
        resolve_setting(NODE, "runtime_host", fields=FIELDS, environ=env)


@pytest.mark.unit
def test_overlay_file_resolution_pin_then_overlay_then_default(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    default.write_text("endpoints: []\n", encoding="utf-8")
    root = tmp_path / "root"
    overlay = _write(root, NODE, "endpoints: []\n")
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("endpoints: []\n", encoding="utf-8")

    assert resolve_overlay_file(NODE, default, env_var="PIN", environ={}) == default
    roots = {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    assert resolve_overlay_file(NODE, default, env_var="PIN", environ=roots) == overlay
    both = {**roots, "PIN": str(pinned)}
    assert resolve_overlay_file(NODE, default, env_var="PIN", environ=both) == pinned


@pytest.mark.unit
def test_a_pin_naming_a_missing_file_refuses_instead_of_falling_through(
    tmp_path: Path,
) -> None:
    default = tmp_path / "default.yaml"
    default.write_text("endpoints: []\n", encoding="utf-8")
    env = {"PIN": str(tmp_path / "missing.yaml")}
    with pytest.raises(NodeOverlayError, match="PIN"):
        resolve_overlay_file(NODE, default, env_var="PIN", environ=env)
