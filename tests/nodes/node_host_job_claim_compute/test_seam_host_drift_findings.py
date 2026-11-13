# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test (OMN-24241): the job claim check and the host drift check print one finding shape.

Both runtimes print ``DRIFT <host> <kind> <subject>`` lines and a ``VERDICT <host>: ...`` line, and an
alarm or a person reads them with one pattern. The kinds of the two checks must stay disjoint so a
line names which check raised it. Each side is run through its own ``main``; a change of either
line's shape, or a kind that appears in both enums, turns this red. The last test shows it red on a
named break (the job check printing its finding without the host).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_host_profile import (
    EnumDriftKind,
    ModelDriftFinding,
    ModelHostDriftCheckResult,
    ModelHostMeasurement,
)
from omnimarket.nodes.node_host_drift_check_effect import runtime_host_drift_check
from omnimarket.nodes.node_host_job_claim_compute import runtime_host_job_claim
from omnimarket.nodes.node_host_job_claim_compute.models import (
    EnumHostJobFindingKind,
    ModelInstalledUnit,
)

pytestmark = pytest.mark.unit

DRIFT_LINE = re.compile(r"^DRIFT (?P<host>\S+) (?P<kind>[a-z_]+) (?P<subject>.+)$")
VERDICT_LINE = re.compile(
    r"^VERDICT (?P<host>\S+): (?P<state>IN_SYNC|DRIFTED) findings=(?P<n>\d+)$"
)

OVERLAY = """\
unit_dirs: [~/units]
unit_suffix: .service
roles:
  - role: seam-role
    jobs:
      - job_id: seam-job
        owner_unit: seam-owner
        owner_command_marker: seam-marker
        claimants: [seam-owner, seam-old]
hosts:
  - {host: seam-host, role: seam-role}
"""


class FakePort:
    def installed(
        self, unit_dirs: tuple[str, ...], suffix: str
    ) -> tuple[ModelInstalledUnit, ...]:
        return (
            ModelInstalledUnit(name="seam-owner", command="run seam-marker"),
            ModelInstalledUnit(name="seam-old", command="run old"),
        )


def job_check_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> list[str]:
    root = tmp_path / "root"
    (root / "node_host_job_claim_compute").mkdir(parents=True)
    (root / "node_host_job_claim_compute" / "overlay.yaml").write_text(OVERLAY)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    assert runtime_host_job_claim.main(["--host", "seam-host"], port=FakePort()) == 1
    return capsys.readouterr().out.splitlines()


def drift_check_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> list[str]:
    measurement = ModelHostMeasurement(
        host="seam-host",
        git_dirs_in_registry=(),
        present_clone_paths=(),
        present_venvs=(),
        units=(),
    )
    result = ModelHostDriftCheckResult(
        host="seam-host",
        ok=False,
        findings=(
            ModelDriftFinding(kind=EnumDriftKind.UNDECLARED_UNIT, subject="seam-x"),
        ),
        measurement=measurement,
    )

    class FakeCheck:
        def __init__(self, *_: Any) -> None: ...

        def handle(self, _request: Any) -> ModelHostDriftCheckResult:
            return result

    class FakeProfile:
        def model_copy(self, **_: Any) -> FakeProfile:
            return self

    monkeypatch.setattr(runtime_host_drift_check, "HandlerHostDriftCheck", FakeCheck)
    monkeypatch.setattr(
        runtime_host_drift_check, "parse_host_profile", lambda *_: FakeProfile()
    )
    monkeypatch.setattr(
        runtime_host_drift_check, "ModelHostDriftCheckRequest", lambda profile: profile
    )
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("x: 1\n")
    assert (
        runtime_host_drift_check.main(
            ["--host", "seam-host", "--profiles", str(profiles)]
        )
        == 1
    )
    return capsys.readouterr().out.splitlines()


def test_both_checks_print_lines_one_pattern_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for lines in (
        job_check_lines(monkeypatch, tmp_path, capsys),
        drift_check_lines(monkeypatch, tmp_path, capsys),
    ):
        *drift, verdict = lines
        assert drift
        for line in drift:
            match = DRIFT_LINE.match(line)
            assert match is not None, line
            assert match["host"] == "seam-host"
        assert VERDICT_LINE.match(verdict), verdict


def test_job_check_names_the_kind_job_extra_unit_and_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = job_check_lines(monkeypatch, tmp_path, capsys)
    assert lines[0] == "DRIFT seam-host second_unit seam-job:seam-old owner=seam-owner"
    assert lines[-1] == "VERDICT seam-host: DRIFTED findings=1"


def test_the_two_checks_never_share_a_kind() -> None:
    job_kinds = {kind.value for kind in EnumHostJobFindingKind}
    drift_kinds = {kind.value for kind in EnumDriftKind}
    assert job_kinds == {"second_unit", "second_implementation"}
    assert job_kinds.isdisjoint(drift_kinds)


def test_failing_control_a_line_without_the_host_is_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Injected break: the job check prints its finding without the host field."""
    line = job_check_lines(monkeypatch, tmp_path, capsys)[0]
    broken = line.replace(" seam-host", "", 1)
    good = DRIFT_LINE.match(line)
    assert good is not None
    assert good["kind"] == "second_unit"
    match = DRIFT_LINE.match(broken)
    assert match is None or match["host"] != "seam-host"
