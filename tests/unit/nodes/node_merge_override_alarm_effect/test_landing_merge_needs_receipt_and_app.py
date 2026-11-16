# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: a receipt for the merged head makes a landing merge; under S19 the merger must be the App."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.store_merge_override import (
    MergeOverrideStore,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideVerdict,
    ModelMergeOverrideReceipt,
    ModelMergeOverrideResult,
    ModelMergeOverrideVerdictEvent,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    APP,
    HAND,
    HEAD,
    OTHER_HEAD,
    PR,
    REPO,
    SETTINGS,
    SLACK_SUFFIX,
    arm_receipt,
    drive_merge,
    make_rig,
    pr_state,
)

pytestmark = pytest.mark.unit
LANDING = EnumMergeOverrideVerdict.LANDING
WITHOUT = EnumMergeOverrideVerdict.MERGE_WITHOUT_OVERRIDE


def _verdict(result: ModelMergeOverrideResult) -> ModelMergeOverrideVerdictEvent:
    assert result.verdict is not None
    return result.verdict


def test_landing_merge_needs_receipt_and_app_receipt_for_the_head_is_landing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    assert rig.handler.handle(arm_receipt()).outcome.value == "receipt_recorded"
    verdict = _verdict(drive_merge(rig))
    assert verdict.verdict is LANDING
    assert verdict.receipt_seen is True
    assert rig.publisher.on(SLACK_SUFFIX) == []
    assert rig.rows("merge_override_open_findings") == []
    assert rig.reader.calls == 0, "a landing merge needs no ledger read"


def test_landing_merge_needs_receipt_and_app_enqueue_is_a_receipt_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(arm_receipt(operation=EnumPrLandingGithubOperation.ENQUEUE))
    assert _verdict(drive_merge(rig)).verdict is LANDING


def test_landing_merge_needs_receipt_and_app_a_receipt_for_another_head_is_not_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(arm_receipt(OTHER_HEAD))
    verdict = _verdict(drive_merge(rig))
    assert verdict.verdict is WITHOUT
    assert verdict.receipt_seen is True


def test_landing_merge_needs_receipt_and_app_a_dry_run_is_not_a_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    ignored = rig.handler.handle(arm_receipt(mode=EnumPrLandingGithubMode.DRY_RUN))
    assert ignored.outcome.value == "ignored"
    assert _verdict(drive_merge(rig)).verdict is WITHOUT


def test_landing_merge_needs_receipt_and_app_newest_receipt_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(arm_receipt(HEAD))
    rig.clock.advance(60)
    rig.handler.handle(arm_receipt(OTHER_HEAD))
    # The newest receipt names the other head, so the merged head has none.
    assert _verdict(drive_merge(rig)).verdict is WITHOUT


def test_landing_merge_needs_receipt_and_app_older_arrival_never_replaces_newer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    store = MergeOverrideStore(rig.database)
    newer = ModelMergeOverrideReceipt(
        repository=REPO,
        pr_number=PR,
        head_sha=HEAD,
        operation="arm_auto_merge",
        observed_at=datetime.fromtimestamp(rig.clock(), tz=UTC),
        correlation_id=uuid4(),
    )
    older = newer.model_copy(
        update={
            "head_sha": OTHER_HEAD,
            "observed_at": newer.observed_at - timedelta(seconds=30),
        }
    )
    assert store.record_receipt(newer) is True
    assert store.record_receipt(older) is False
    kept = store.newest_receipt(REPO, PR)
    assert kept is not None
    assert kept.head_sha == HEAD


@pytest.mark.parametrize(
    ("merger", "expected"),
    [(APP, LANDING), (HAND, WITHOUT), (None, WITHOUT)],
)
def test_landing_merge_needs_receipt_and_app_s19_repository_judges_the_merger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    merger: str | None,
    expected: EnumMergeOverrideVerdict,
) -> None:
    rig = make_rig(
        monkeypatch, tmp_path, settings={**SETTINGS, "s19_repositories": [REPO]}
    )
    rig.handler.handle(arm_receipt())
    verdict = _verdict(drive_merge(rig, state=pr_state(merger=merger)))
    assert verdict.verdict is expected


def test_landing_merge_needs_receipt_and_app_other_repository_ignores_the_merger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(
        monkeypatch,
        tmp_path,
        settings={**SETTINGS, "s19_repositories": ["OmniNode-ai/other-repo"]},
    )
    rig.handler.handle(arm_receipt())
    assert _verdict(drive_merge(rig, state=pr_state(merger=HAND))).verdict is LANDING
