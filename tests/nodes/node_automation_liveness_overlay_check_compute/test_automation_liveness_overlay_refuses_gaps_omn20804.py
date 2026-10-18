# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay check refuses gaps (OMN-20804, plan 4.11, AC2).

Each case plants exactly one defect in an otherwise valid overlay and tree, and the positive
control is the same tree without the defect passing with the counts it should have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_automation_liveness_overlay_check_compute.models import (
    EnumAutomationLivenessFindingKind,
)
from tests.nodes.node_automation_liveness_overlay_check_compute.support import (
    REPOSITORY,
    entry_yaml,
    overlay_yaml,
    run_check,
    sources_yaml,
    write,
)

pytestmark = pytest.mark.unit

_Kind = EnumAutomationLivenessFindingKind
_UNIT = "deploy/units/onex-planted.timer"


def _tree(tmp_path: Path) -> Path:
    write(tmp_path, f"{REPOSITORY}/{_UNIT}", "[Timer]\nOnCalendar=hourly\n")
    return tmp_path


def test_automation_liveness_overlay_refuses_gaps_positive_control(
    tmp_path: Path,
) -> None:
    result = run_check(overlay_yaml(entry_yaml()), sources_yaml(), _tree(tmp_path))
    assert result.findings == ()
    assert result.entries_checked == 1
    assert result.sources_found == 1


def test_automation_liveness_overlay_refuses_gaps_new_unit_without_entry(
    tmp_path: Path,
) -> None:
    other = entry_yaml("onex-other")

    result = run_check(overlay_yaml(other), sources_yaml(), _tree(tmp_path))

    assert [finding.kind for finding in result.findings] == [_Kind.UNDECLARED_SOURCE]
    assert _UNIT in result.findings[0].subject


def test_automation_liveness_overlay_refuses_gaps_entry_with_no_positive_control(
    tmp_path: Path,
) -> None:
    result = run_check(
        overlay_yaml(entry_yaml(drop=("positive_control",))),
        sources_yaml(),
        _tree(tmp_path),
    )

    assert [finding.kind for finding in result.findings] == [_Kind.OVERLAY_INVALID]
    assert result.findings[0].subject == "h201/onex-planted"
    assert "positive_control" in result.findings[0].detail


def test_automation_liveness_overlay_refuses_gaps_entry_with_an_empty_positive_control(
    tmp_path: Path,
) -> None:
    empty = entry_yaml(positive_control='{kind: class_drill, ref: ""}')

    result = run_check(overlay_yaml(empty), sources_yaml(), _tree(tmp_path))

    assert [finding.kind for finding in result.findings] == [_Kind.OVERLAY_INVALID]
    assert "positive_control" in result.findings[0].detail


def test_automation_liveness_overlay_refuses_gaps_broken_deadline(
    tmp_path: Path,
) -> None:
    # 2 intervals of 600 s plus the monitor's 420 s overhead is 1620 s; one second less breaks it.
    broken = entry_yaml(alarm_deadline_seconds="1619")

    result = run_check(overlay_yaml(broken), sources_yaml(), _tree(tmp_path))

    assert [finding.kind for finding in result.findings] == [_Kind.OVERLAY_INVALID]
    assert result.findings[0].subject == "h201/onex-planted"
    assert "worst case" in result.findings[0].detail
    assert run_check(overlay_yaml(entry_yaml()), sources_yaml(), _tree(tmp_path)).passed


def test_automation_liveness_overlay_refuses_gaps_cron_entry_without_completion_record(
    tmp_path: Path,
) -> None:
    cron = entry_yaml(
        "planted-cron",
        native_id="crontab:planted-cron",
        kind="cron",
        evidence="{source: log_line, locator: /var/log/planted.log}",
    )
    result = run_check(overlay_yaml(cron), sources_yaml(), tmp_path)
    assert [finding.kind for finding in result.findings] == [_Kind.OVERLAY_INVALID]
    assert "completion" in result.findings[0].detail


def test_automation_liveness_overlay_refuses_gaps_class_drill_for_another_trigger_kind(
    tmp_path: Path,
) -> None:
    wrong = entry_yaml(positive_control='{kind: class_drill, ref: "class-drill/cron"}')

    result = run_check(overlay_yaml(wrong), sources_yaml(), _tree(tmp_path))

    assert [finding.kind for finding in result.findings] == [
        _Kind.POSITIVE_CONTROL_MISMATCH
    ]
    assert "class-drill/systemd-timer" in result.findings[0].detail


def test_automation_liveness_overlay_refuses_gaps_replay_fixture_outside_its_namespace(
    tmp_path: Path,
) -> None:
    wrong = entry_yaml(
        positive_control='{kind: replay_fixture, ref: "fixtures/lab-fill"}'
    )

    result = run_check(overlay_yaml(wrong), sources_yaml(), _tree(tmp_path))

    assert [finding.kind for finding in result.findings] == [
        _Kind.POSITIVE_CONTROL_MISMATCH
    ]


def test_automation_liveness_overlay_refuses_gaps_duplicate_process(
    tmp_path: Path,
) -> None:
    result = run_check(
        overlay_yaml(entry_yaml(), entry_yaml()), sources_yaml(), _tree(tmp_path)
    )
    assert [finding.kind for finding in result.findings] == [_Kind.OVERLAY_INVALID]
    assert "duplicate" in result.findings[0].detail


def test_automation_liveness_overlay_refuses_gaps_stale_declared_source(
    tmp_path: Path,
) -> None:
    declared = sources_yaml(
        (REPOSITORY, "ops/gone/install.sh", ("onex-planted.timer",))
    )

    result = run_check(overlay_yaml(entry_yaml()), declared, _tree(tmp_path))

    assert [finding.kind for finding in result.findings] == [_Kind.STALE_SOURCE]


def test_automation_liveness_overlay_refuses_gaps_not_installed_with_no_installer(
    tmp_path: Path,
) -> None:
    ghost = entry_yaml("onex-ghost", state="not_installed")

    result = run_check(
        overlay_yaml(entry_yaml(), ghost), sources_yaml(), _tree(tmp_path)
    )

    assert [finding.kind for finding in result.findings] == [
        _Kind.NOT_INSTALLED_WITHOUT_INSTALLER
    ]
    assert result.findings[0].subject == "h201/onex-ghost"


def test_automation_liveness_overlay_refuses_gaps_retired_with_an_installer_left(
    tmp_path: Path,
) -> None:
    result = run_check(
        overlay_yaml(entry_yaml(state="retired")), sources_yaml(), _tree(tmp_path)
    )
    assert [finding.kind for finding in result.findings] == [
        _Kind.RETIRED_STILL_INSTALLED
    ]

    (tmp_path / REPOSITORY / _UNIT).unlink()
    deleted = run_check(
        overlay_yaml(entry_yaml(state="retired")), sources_yaml(), tmp_path
    )
    assert deleted.passed


def test_automation_liveness_overlay_refuses_gaps_a_declaration_that_does_not_load(
    tmp_path: Path,
) -> None:
    result = run_check(
        overlay_yaml(entry_yaml()),
        "schema_version: automation-liveness-sources/v1\nrepositories: [a]\nsources: nope\n",
        _tree(tmp_path),
    )
    assert [finding.kind for finding in result.findings] == [_Kind.SOURCES_INVALID]


def test_automation_liveness_overlay_refuses_gaps_a_source_in_an_undeclared_repository(
    tmp_path: Path,
) -> None:
    declared = sources_yaml(("elsewhere", "install.sh", ("onex-planted.timer",)))

    result = run_check(overlay_yaml(entry_yaml()), declared, _tree(tmp_path))

    assert [finding.kind for finding in result.findings] == [_Kind.SOURCES_INVALID]


def test_automation_liveness_overlay_refuses_gaps_a_partial_run_never_reads_as_complete(
    tmp_path: Path,
) -> None:
    result = run_check(
        overlay_yaml(entry_yaml()),
        sources_yaml(repositories=(REPOSITORY, "omniclaude")),
        _tree(tmp_path),
        complete=False,
    )
    assert result.passed
    assert not result.complete
