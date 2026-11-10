# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installer's start check runs with the rendered unit's environment (OMN-23514 child).

The check ran ``<module> --help`` in the installing shell's environment, so a request that named an overlay root only
through its path override rendered a correct unit and was then refused ("does not start from ...; not loaded"). The check
now carries the rendered environment, so what is checked is what will run. The cutover snapshot unit's overlay file is a
request override as well: the overlays clone is not under every registry root.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    _run,
    make_checkout,
    request,
)

pytestmark = pytest.mark.unit

ROOTS = "ONEX_SKILL_OVERLAY_ROOTS"
TICK_ENTRY = "src/omnimarket/nodes/node_pr_landing_tick_orchestrator/__main__.py"
SNAPSHOT_ENTRY = "src/omnimarket/nodes/node_pr_landing_snapshot_effect/__main__.py"


@pytest.fixture(autouse=True)
def brew_python_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.delenv(ROOTS, raising=False)


def fake_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected: str) -> None:
    """A ``uv`` that starts only when the overlay root in its environment is the one the request named."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "uv"
    script.write_text(
        f'#!/bin/sh\nif [ "${ROOTS}" = "{expected}" ]; then exit 0; fi\n'
        'echo "overlay root unset" >&2\nexit 3\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:" + os.environ["PATH"])


class Proofs:
    """Real git and id; the start check (``env ... uv ...``, or bare ``uv ...`` at the base) runs for real."""

    def __init__(self) -> None:
        self.proofs: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] in ("git", "id"):
            return _run(argv, capture_output=True, text=True, check=False)
        if argv[0] == "env" or os.path.basename(argv[0]) == "uv":
            self.proofs.append(argv)
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        return subprocess.CompletedProcess(argv, 0, "", "")


def tick_request(tmp_path: Path, **kw: Any) -> Any:
    sha = make_checkout(tmp_path / "wt", TICK_ENTRY)
    return request(
        tmp_path,
        unit="landing-tick-orchestrator",
        mode="install",
        project_path=str(tmp_path / "wt"),
        expected_revision=sha,
        **kw,
    )


def test_golden_the_start_check_sees_an_overlay_root_only_the_request_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = str(tmp_path / "overlays-clone")
    fake_uv(tmp_path, monkeypatch, clone)
    proofs = Proofs()
    result = HandlerRegistryUnits(proofs).handle(
        tick_request(tmp_path, path_overrides={ROOTS: clone})
    )
    assert result.exit_code == 0, result.stderr
    assert len(proofs.proofs) == 1
    assert f"{ROOTS}={clone}" in proofs.proofs[0]
    assert proofs.proofs[0][-1] == "--help"


def test_error_the_start_check_refuses_when_the_rendered_environment_lacks_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_uv(tmp_path, monkeypatch, str(tmp_path / "overlays-clone"))
    result = HandlerRegistryUnits(Proofs()).handle(tick_request(tmp_path))
    assert result.exit_code != 0
    assert "does not start from" in result.stderr
    assert "overlay root unset" in result.stderr


def test_the_cutover_overlay_file_is_a_request_override(tmp_path: Path) -> None:
    sha = make_checkout(tmp_path / "wt", SNAPSHOT_ENTRY)
    overlay = str(tmp_path / "overlays-clone" / "cutover" / "overlay.yaml")
    result = HandlerRegistryUnits(Proofs()).handle(
        request(
            tmp_path,
            unit="landing-snapshot-cutover",
            mode="dry-run",
            project_path=str(tmp_path / "wt"),
            expected_revision=sha,
            path_overrides={"ONEX_LANDING_SNAPSHOT_OVERLAY_FILE": overlay},
        )
    )
    assert result.exit_code == 0, result.stderr
    environment = plistlib.loads(result.rendered.encode())["EnvironmentVariables"]
    assert environment["ONEX_LANDING_SNAPSHOT_OVERLAY_FILE"] == overlay
