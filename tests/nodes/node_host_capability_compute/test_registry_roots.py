# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment registry roots for the capability probe (OMN-21373)."""

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_capability_compute.handlers.handler_host_capability import (
    capability_probe_sh,
    registry_root_candidates,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def overlay(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "lab_hosts/overlay.yaml"
    path.parent.mkdir()
    path.write_text(
        "hosts: [{name: host-a, target: 192.0.2.1}]\n"
        'registry_root_candidates: ["$HOME/registry-a", "/srv/registry-b"]\n'
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_overlay_resolves_registry_roots_in_order(overlay: dict[str, str]) -> None:
    assert registry_root_candidates(overlay) == ("$HOME/registry-a", "/srv/registry-b")


def test_environment_wins_over_overlay(overlay: dict[str, str]) -> None:
    assert registry_root_candidates(
        {**overlay, "ONEX_REGISTRY_ROOT": "/srv/pinned"}
    ) == ("/srv/pinned",)


def test_explicit_registry_roots_win(overlay: dict[str, str]) -> None:
    script = capability_probe_sh(
        [],
        bound="",
        path_export="",
        registry_roots=["/srv/explicit"],
        environ={**overlay, "ONEX_REGISTRY_ROOT": "/srv/pinned"},
    )
    assert '"/srv/explicit"' in script
    assert "/srv/pinned" not in script
    assert "registry-a" not in script


@pytest.mark.parametrize("roots", [None, []])
def test_missing_or_empty_registry_roots_refuse(roots: list[str] | None) -> None:
    with pytest.raises(NodeOverlayError) as error:
        capability_probe_sh(
            [], bound="", path_export="", registry_roots=roots, environ={}
        )
    message = str(error.value)
    assert "lab_hosts.registry_root_candidates" in message
    assert "ONEX_REGISTRY_ROOT" in message
    assert "ONEX_SKILL_OVERLAY_ROOTS" in message


@pytest.mark.parametrize(
    "candidate", ['x"; rm', "`id`", "$(id)", "with space", "", "/srv/ok\n"]
)
def test_bad_explicit_candidate_refuses(candidate: str) -> None:
    with pytest.raises(NodeOverlayError, match="registry_root_candidates"):
        capability_probe_sh(
            [], bound="", path_export="", registry_roots=[candidate], environ={}
        )


@pytest.mark.parametrize(
    "value", ["[]", "null", '"/srv/root"', '[""]', "[42]", "['x\"; rm']"]
)
def test_bad_overlay_names_path_and_field(overlay: dict[str, str], value: str) -> None:
    path = Path(overlay["ONEX_SKILL_OVERLAY_ROOTS"]) / "lab_hosts/overlay.yaml"
    path.write_text("registry_root_candidates: " + value + "\n")
    with pytest.raises(NodeOverlayError) as error:
        registry_root_candidates(overlay)
    assert str(path) in str(error.value)
    assert "registry_root_candidates" in str(error.value)


def test_unreadable_overlay_names_path_and_exception_only(
    overlay: dict[str, str],
) -> None:
    path = Path(overlay["ONEX_SKILL_OVERLAY_ROOTS"]) / "lab_hosts/overlay.yaml"
    path.write_bytes(b"\xff")
    with pytest.raises(NodeOverlayError) as error:
        registry_root_candidates(overlay)
    assert str(error.value) == (
        f"ONEX_SKILL_OVERLAY_ROOTS: overlay {path} is unreadable (UnicodeDecodeError)"
    )


def test_rendered_probe_quotes_registry_roots(overlay: dict[str, str]) -> None:
    script = capability_probe_sh([], bound="", path_export="", environ=overlay)
    assert (
        'else for r in "${PARITY_H:-}" "${ONEX_REGISTRY_ROOT:-}" "$HOME/registry-a" "/srv/registry-b"; do'
        in script
    )
    assert "OMNI" + "_HOME" not in script
    assert "omni" + "_home" not in script


def test_overlay_without_registry_roots_names_configuration_sources(
    overlay: dict[str, str],
) -> None:
    path = Path(overlay["ONEX_SKILL_OVERLAY_ROOTS"]) / "lab_hosts/overlay.yaml"
    path.write_text("hosts: [{name: host-a, target: 192.0.2.1}]\n")
    with pytest.raises(NodeOverlayError) as error:
        registry_root_candidates(overlay)
    message = str(error.value)
    assert str(path) in message
    assert "lab_hosts.registry_root_candidates" in message
    assert "ONEX_REGISTRY_ROOT" in message
    assert "ONEX_SKILL_OVERLAY_ROOTS" in message
