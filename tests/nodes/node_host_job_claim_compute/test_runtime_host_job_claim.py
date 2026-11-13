# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime CLI tests of the host job claim read.

main with a fake port: a duplicate gives exit 1 and a DRIFT line; no duplicate gives 0 and
VERDICT IN_SYNC; an unknown host gives 2 and REFUSED on stderr.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_job_claim_compute.models import ModelInstalledUnit
from omnimarket.nodes.node_host_job_claim_compute.runtime_host_job_claim import main

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_job_claim_compute/overlay.example.yaml"
)


class FakePort:
    def __init__(self, units: tuple[ModelInstalledUnit, ...]) -> None:
        self._units = units

    def installed(
        self,
        unit_dirs: tuple[str, ...],
        suffix: str,
    ) -> tuple[ModelInstalledUnit, ...]:
        return self._units


def _setup_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay_dir = tmp_path / "node_host_job_claim_compute"
    overlay_dir.mkdir()
    shutil.copy(EXAMPLE, overlay_dir / "overlay.yaml")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def test_runtime_duplicate_gives_exit_1_and_drift_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_overlay(tmp_path, monkeypatch)
    port = FakePort(
        (
            ModelInstalledUnit(name="example-unit", command="/bin/example-marker"),
            ModelInstalledUnit(name="example-unit-old", command="/bin/old"),
        )
    )
    exit_code = main(["--host", "example-host"], port=port)
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "DRIFT example-host second_unit" in out
    assert "example-unit-old" in out


def test_runtime_no_duplicate_gives_exit_0_and_in_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_overlay(tmp_path, monkeypatch)
    port = FakePort(
        (ModelInstalledUnit(name="example-unit", command="/bin/example-marker"),)
    )
    exit_code = main(["--host", "example-host"], port=port)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "VERDICT example-host: IN_SYNC findings=0" in out


def test_runtime_unknown_host_gives_exit_2_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_overlay(tmp_path, monkeypatch)
    port = FakePort(())
    exit_code = main(["--host", "unknown-host"], port=port)
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
