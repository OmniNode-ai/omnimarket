# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check run from its new home: overlay and sources in a separate overlays checkout (OMN-20804).

The overlay and the sources declaration moved out of the private code repository into the overlays
repository, so the check is handed them by path (``--overlay``, ``--sources``) and finds the
installers in registry clones that hold neither. Each case plants that layout: an overlays checkout
that is not a scanned repository and a registry directory holding a fixture installer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_automation_liveness_overlay_check_effect.runtime_automation_liveness_overlay_check_cli import (
    main,
)
from tests.nodes.node_automation_liveness_overlay_check_compute.support import (
    PLANTED_LAUNCHD_INSTALLER,
    REPOSITORY,
    entry_yaml,
    overlay_yaml,
    sources_yaml,
    write,
)

pytestmark = pytest.mark.unit

_INSTALLER_PATH = "ops/odd/place/setup-planted.sh"
_LABEL = "ai.omninode.planted"
_OVERLAY_FILE = "overlays/automation_liveness/overlay.yaml"
_SOURCES_FILE = "overlays/automation_liveness_sources/overlay.yaml"


def _entry() -> str:
    return entry_yaml(
        "planted",
        native_id=_LABEL,
        kind="launchd-interval",
        evidence="{source: launchd_state, locator: ai.omninode.planted}",
        run_record="latest-only",
        expected_interval_seconds="600",
        alarm_deadline_seconds="1620",
    )


def _layout(tmp_path: Path, sources_text: str) -> list[str]:
    overlays = tmp_path / "overlays_checkout"
    write(overlays, _OVERLAY_FILE, overlay_yaml(_entry()))
    write(overlays, _SOURCES_FILE, sources_text)
    write(
        tmp_path, f"registry/{REPOSITORY}/{_INSTALLER_PATH}", PLANTED_LAUNCHD_INSTALLER
    )
    return [
        "--internal-root",
        str(overlays),
        "--private-repository",
        "overlays",
        "--registry-root",
        str(tmp_path / "registry"),
        "--overlay",
        str(overlays / _OVERLAY_FILE),
        "--sources",
        str(overlays / _SOURCES_FILE),
    ]


def test_automation_liveness_installer_scan_moved_layout_refuses_an_undeclared_fixture_installer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _layout(tmp_path, sources_yaml())

    assert main(arguments) == 1
    printed = capsys.readouterr().out
    assert f"undeclared_source: {REPOSITORY}/{_INSTALLER_PATH}" in printed
    assert "1 findings" in printed


def test_automation_liveness_installer_scan_moved_layout_passes_once_the_declaration_is_in_the_overlays_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    declared = sources_yaml((REPOSITORY, _INSTALLER_PATH, (_LABEL,)))
    arguments = _layout(tmp_path, declared)

    assert main(arguments) == 0
    assert "0 findings, scope complete" in capsys.readouterr().out


def test_automation_liveness_installer_scan_moved_layout_refuses_a_declaration_for_a_file_that_is_gone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    declared = sources_yaml(
        (REPOSITORY, _INSTALLER_PATH, (_LABEL,)),
        (REPOSITORY, "ops/gone/install-gone.sh", (_LABEL,)),
    )
    arguments = _layout(tmp_path, declared)

    assert main(arguments) == 1
    printed = capsys.readouterr().out
    assert "ops/gone/install-gone.sh" in printed
