# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installer scan finds writers wherever they live (OMN-20804, plan 4.11, AC3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.handler_automation_liveness_overlay_check_cli import main
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers import (
    classify_source,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.models import (
    EnumAutomationLivenessFindingKind,
    EnumAutomationLivenessSourceClass,
)
from tests.nodes.node_automation_liveness_overlay_check_compute.support import (
    PLANTED_LAUNCHD_INSTALLER,
    REPOSITORY,
    entry_yaml,
    overlay_yaml,
    run_check,
    sources_yaml,
    write,
)

pytestmark = pytest.mark.unit

_Kind = EnumAutomationLivenessFindingKind
_Class = EnumAutomationLivenessSourceClass
_PLANTED_PATH = "ops/odd/place/setup-planted.sh"
_PLANTED_LABEL = "ai.omninode.planted"


def _launchd_entry() -> str:
    return entry_yaml(
        "planted",
        native_id=_PLANTED_LABEL,
        kind="launchd-interval",
        evidence="{source: launchd_state, locator: ai.omninode.planted}",
        run_record="latest-only",
        expected_interval_seconds="600",
        alarm_deadline_seconds="1620",
    )


def test_automation_liveness_installer_scan_finds_a_planted_installer_in_an_unlisted_directory(
    tmp_path: Path,
) -> None:
    write(tmp_path, f"{REPOSITORY}/{_PLANTED_PATH}", PLANTED_LAUNCHD_INSTALLER)
    overlay = overlay_yaml(_launchd_entry())

    refused = run_check(overlay, sources_yaml(), tmp_path)

    assert not refused.passed
    kinds = {finding.kind for finding in refused.findings}
    assert kinds == {_Kind.UNDECLARED_SOURCE}
    assert refused.sources_found == 1
    assert _PLANTED_PATH in refused.findings[0].subject

    declared = sources_yaml((REPOSITORY, _PLANTED_PATH, (_PLANTED_LABEL,)))
    assert run_check(overlay, declared, tmp_path).passed


def test_automation_liveness_installer_scan_refuses_a_declared_unit_with_no_entry(
    tmp_path: Path,
) -> None:
    write(tmp_path, f"{REPOSITORY}/{_PLANTED_PATH}", PLANTED_LAUNCHD_INSTALLER)
    declared = sources_yaml((REPOSITORY, _PLANTED_PATH, (_PLANTED_LABEL,)))

    result = run_check(overlay_yaml(entry_yaml("other")), declared, tmp_path)

    assert {finding.kind for finding in result.findings} == {_Kind.UNDECLARED_UNIT}


def test_automation_liveness_installer_scan_finds_the_cli_exit_code(
    tmp_path: Path,
) -> None:
    internal = tmp_path / "private_repo"
    overlays = internal / "src/private_repo/overlays"
    write(
        tmp_path,
        "private_repo/ops/hidden/deep/install.sh",
        PLANTED_LAUNCHD_INSTALLER,
    )
    write(
        internal,
        "src/private_repo/overlays/automation_liveness_overlay.yaml",
        overlay_yaml(_launchd_entry()),
    )
    sources_file = overlays / "automation_liveness_sources.yaml"
    write(
        internal,
        "src/private_repo/overlays/automation_liveness_sources.yaml",
        sources_yaml(repositories=("private_repo",)),
    )
    arguments = ["--internal-root", str(internal), "--internal-only"]

    assert main(arguments) == 1

    sources_file.write_text(
        sources_yaml(
            ("private_repo", "ops/hidden/deep/install.sh", (_PLANTED_LABEL,)),
            repositories=("private_repo",),
        ),
        encoding="utf-8",
    )
    assert main(arguments) == 0


def test_automation_liveness_installer_scan_refuses_a_run_that_did_not_scan_a_repository(
    tmp_path: Path,
) -> None:
    result = run_check(
        overlay_yaml(entry_yaml()),
        sources_yaml(repositories=(REPOSITORY, "omniclaude")),
        tmp_path,
        repositories=(REPOSITORY,),
    )
    assert {finding.kind for finding in result.findings} == {
        _Kind.REPOSITORY_NOT_SCANNED
    }


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        ("units/onex-x.timer", "[Timer]\n", _Class.SYSTEMD_UNIT_FILE),
        ("units/onex-x.service", "[Service]\n", _Class.SYSTEMD_UNIT_FILE),
        ("a/ai.omninode.x.plist", "<plist/>", _Class.LAUNCHD_PLIST),
        ("a/ai.omninode.x.plist.tmpl", "<plist/>", _Class.LAUNCHD_PLIST),
        ("a/com.omninode.y.plist.template", "<plist/>", _Class.LAUNCHD_PLIST),
        ("deploy/cron.d/omninode-job", "17 * * * * root run\n", _Class.CRON_D_FILE),
        ("a/install.sh", 'launchctl load "$PLIST"\n', _Class.INSTALLER_CODE),
        (
            "a/install.sh",
            'launchctl bootstrap "gui/501" "$PLIST"\n',
            _Class.INSTALLER_CODE,
        ),
        ("a/install.sh", "systemctl --user daemon-reload\n", _Class.INSTALLER_CODE),
        ("a/install.sh", "systemctl link /opt/x.service\n", _Class.INSTALLER_CODE),
        ("a/install.sh", "crontab -\n", _Class.INSTALLER_CODE),
        ("a/install.sh", "crontab /tmp/mine\n", _Class.INSTALLER_CODE),
        ("a/install.sh", "cp x.service /etc/systemd/system/\n", _Class.INSTALLER_CODE),
        (
            "a/install.sh",
            'cat > "$HOME/.config/systemd/user/x.service"\n',
            _Class.INSTALLER_CODE,
        ),
        (
            "a/w.py",
            'subprocess.run(["launchctl", "bootstrap", d, p])\n',
            _Class.INSTALLER_CODE,
        ),
        (
            "a/w.py",
            'run(["systemctl", "--user", "daemon-reload"])\n',
            _Class.INSTALLER_CODE,
        ),
        (
            "a/w.py",
            'p = Path(".config/systemd/user") / n\np.write_text(t)\n',
            _Class.INSTALLER_CODE,
        ),
        ("a/tool", "#!/bin/sh\nlaunchctl load x\n", _Class.INSTALLER_CODE),
    ],
)
def test_automation_liveness_installer_scan_vocabulary_finds(
    path: str, text: str, expected: EnumAutomationLivenessSourceClass
) -> None:
    found = classify_source(REPOSITORY, path, text)
    assert found is not None
    assert found.source_class is expected


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("a/read.sh", "crontab -l\nlaunchctl list\nsystemctl status x\n"),
        ("a/read.sh", "# launchctl load x\necho hi\n"),
        ("a/read.sh", "ls /etc/cron.d >/dev/null 2>&1\n"),
        ("a/read.py", 'p = Path(".config/systemd/user") / n\nprint(p.read_text())\n'),
        ("a/typed.py", 'def f() -> "Path":\n    return Path("/etc/systemd/system")\n'),
        ("tests/install.sh", 'launchctl load "$PLIST"\n'),
        ("docs/install.sh", 'launchctl load "$PLIST"\n'),
        ("a/.hidden/install.sh", 'launchctl load "$PLIST"\n'),
        ("a/test_install.py", 'run(["launchctl", "load", p])\n'),
        ("a/boot.sh.tmpl", "systemctl link x\nlaunchctl load y\n"),
        ("a/notes.md", "launchctl load x\n"),
        ("a/unit.conf", "[Service]\n"),
    ],
)
def test_automation_liveness_installer_scan_vocabulary_ignores(
    path: str, text: str
) -> None:
    assert classify_source(REPOSITORY, path, text) is None


def test_automation_liveness_installer_scan_does_not_read_its_own_module_as_an_installer() -> (
    None
):
    package = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
    for module in sorted(
        package.glob("node_automation_liveness_overlay_check_compute/**/*.py")
    ):
        relative = module.relative_to(package.parents[2]).as_posix()
        assert (
            classify_source("omnimarket", relative, module.read_text("utf-8")) is None
        ), relative


def test_automation_liveness_installer_scan_requires_every_unit_a_drift_manifest_names(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        f"{REPOSITORY}/deploy/unit-drift-manifest.yaml",
        "units:\n  - name: onex-planted.timer\n  - name: onex-planted.service\n"
        "  - name: onex-missing.timer\n",
    )
    overlay = overlay_yaml(entry_yaml())

    result = run_check(overlay, sources_yaml(), tmp_path)

    assert [finding.kind for finding in result.findings] == [_Kind.UNDECLARED_UNIT]
    assert "onex-missing.timer" in result.findings[0].detail
    covered = overlay_yaml(entry_yaml(), entry_yaml("onex-missing"))
    assert run_check(covered, sources_yaml(), tmp_path).passed


def test_automation_liveness_installer_scan_reads_the_host_drift_manifest_roles(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        f"{REPOSITORY}/host_drift_manifest.yaml",
        "roles:\n  linux-lab:\n    units:\n"
        "      - {name: onex-planted, kind: systemd-timer}\n"
        "      - {name: onex-gone, kind: systemd-timer}\n"
        "  macos-lab:\n    units:\n"
        "      - {name: ai.omninode.mac-unit, kind: launchd}\n",
    )

    result = run_check(overlay_yaml(entry_yaml()), sources_yaml(), tmp_path)

    missing = sorted(finding.detail.split("'")[1] for finding in result.findings)
    assert missing == ["ai.omninode.mac-unit", "onex-gone"]
