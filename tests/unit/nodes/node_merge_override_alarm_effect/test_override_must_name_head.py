# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: an override row names the pull request and the merged head; another head does not match."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_merge_override_alarm_effect.handlers.match_override import (
    match_override,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideVerdict,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    FINDING_SUFFIX,
    HEAD,
    OTHER_HEAD,
    PR,
    REPO,
    SLACK_SUFFIX,
    drive_merge,
    make_rig,
    override_row,
    ruling_row,
)

pytestmark = pytest.mark.unit


def test_override_must_name_head_matching_row_is_override_merge_naming_the_ruling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = override_row()
    rig = make_rig(monkeypatch, tmp_path, rows=[row])
    result = drive_merge(rig)
    assert result.verdict is not None
    assert result.verdict.verdict is EnumMergeOverrideVerdict.OVERRIDE_MERGE
    assert result.verdict.ruling_row_id == row.row_id
    assert rig.publisher.on(SLACK_SUFFIX) == []
    assert rig.publisher.on(FINDING_SUFFIX) == []
    assert rig.rows("merge_override_open_findings") == []


def test_override_must_name_head_a_row_naming_another_head_does_not_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, rows=[override_row(OTHER_HEAD)])
    result = drive_merge(rig)
    assert result.verdict is not None
    assert result.verdict.verdict is EnumMergeOverrideVerdict.MERGE_WITHOUT_OVERRIDE
    assert rig.reader.calls == 1
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


def test_override_must_name_head_short_repository_form_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, rows=[override_row(pr=f"omnimarket#{PR}")])
    result = drive_merge(rig)
    assert result.verdict is not None
    assert result.verdict.verdict is EnumMergeOverrideVerdict.OVERRIDE_MERGE


@pytest.mark.parametrize(
    "cells",
    [
        f"override=merge | pr={REPO}#99 | head={HEAD}",
        f"override=hold | pr={REPO}#{PR} | head={HEAD}",
        f"pr={REPO}#{PR} | head={HEAD}",
        f"override=merge | pr={REPO}#{PR}",
        f"override=merge | pr={REPO}#{PR} | head={HEAD[:12]}",
        f"override=merge | pr=OmniNode-ai/other#{PR} | head={HEAD}",
        f"override=merge | pr={REPO}#{PR} | head={HEAD.upper()}",
    ],
)
def test_override_must_name_head_unrelated_or_partial_rows_never_match(
    cells: str,
) -> None:
    assert match_override([ruling_row(cells)], REPO, PR, HEAD) is None


def test_override_must_name_head_only_ruling_rows_count() -> None:
    row = ruling_row(f"override=merge | pr={REPO}#{PR} | head={HEAD}", row_type="MSG")
    assert match_override([row], REPO, PR, HEAD) is None


def test_override_must_name_head_earliest_matching_row_is_named() -> None:
    early = ruling_row(
        f"override=merge | pr={REPO}#{PR} | head={HEAD}", age_seconds=7200
    )
    late = ruling_row(f"override=merge | pr={REPO}#{PR} | head={HEAD}", age_seconds=60)
    assert match_override([late, early], REPO, PR, HEAD) == early
