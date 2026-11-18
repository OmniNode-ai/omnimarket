# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay tests of the host job claim read.

With ONEX_SKILL_OVERLAY_ROOTS unset the load is refused; an overlay missing roles, an empty
roles list, a host assigned an undeclared role, and a host listed twice are each refused; the
shipped overlay.example.yaml loads when copied into the tmp root.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_job_claim_compute.overlay import load_overlay

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_job_claim_compute/overlay.example.yaml"
)


def test_overlay_load_refused_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(NodeOverlayError):
        load_overlay()


def test_overlay_missing_roles_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_dir = tmp_path / "node_host_job_claim_compute"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        "hosts:\n- host: h\n  role: r\nunit_dirs:\n- /tmp\nunit_suffix: .service\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_overlay()


def test_overlay_empty_roles_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_dir = tmp_path / "node_host_job_claim_compute"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        "roles: []\nhosts:\n- host: h\n  role: r\nunit_dirs:\n- /tmp\nunit_suffix: .service\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_overlay()


def test_overlay_host_undeclared_role_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_dir = tmp_path / "node_host_job_claim_compute"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        "roles:\n- role: r\n  jobs:\n  - job_id: j\n    owner_unit: u\n    owner_command_marker: m\n    claimants:\n    - u\nhosts:\n- host: h\n  role: undeclared\nunit_dirs:\n- /tmp\nunit_suffix: .service\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_overlay()


def test_overlay_host_listed_twice_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_dir = tmp_path / "node_host_job_claim_compute"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        "roles:\n- role: r\n  jobs:\n  - job_id: j\n    owner_unit: u\n    owner_command_marker: m\n    claimants:\n    - u\nhosts:\n- host: h\n  role: r\n- host: h\n  role: r\nunit_dirs:\n- /tmp\nunit_suffix: .service\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_overlay()


def test_example_overlay_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay_dir = tmp_path / "node_host_job_claim_compute"
    overlay_dir.mkdir()
    shutil.copy(EXAMPLE, overlay_dir / "overlay.yaml")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    overlay = load_overlay()
    assert overlay.roles[0].role == "example-role"
    assert overlay.hosts[0].host == "example-host"
