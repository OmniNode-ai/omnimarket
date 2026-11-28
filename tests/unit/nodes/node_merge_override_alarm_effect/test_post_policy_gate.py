# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge override alarm's Slack command goes through the channel post policy (OMN-24562).

The two recorded messages: "Merge without override: <pr> ..." (a reading, not a page) and "Merge override detector
unreadable for <pr> ..." (a detector fault the person cannot act on until it has been blind for hours).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.alarm_post_policy_source import PostPolicySource
from omnimarket.models.alarm_post_policy import ModelAlarmPostPolicy
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    SETTINGS,
    SLACK_SUFFIX,
    drive_merge,
    make_rig,
)

pytestmark = pytest.mark.unit

_OVERLAY = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect/overlay.example.yaml"
)


def _policy() -> dict[str, object]:
    return dict(yaml.safe_load(_OVERLAY.read_text(encoding="utf-8"))["post_policy"])


def _settings(tmp_path: Path) -> dict[str, object]:
    return {
        **SETTINGS,
        "post_policy": _policy(),
        "post_policy_state_file": str(tmp_path / "gate.json"),
    }


def test_merge_without_override_posts_nothing_under_the_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, settings=_settings(tmp_path))
    result = drive_merge(rig)
    assert result.finding_published
    assert result.command_published
    assert not result.error
    assert rig.publisher.on(SLACK_SUFFIX) == []


def test_without_the_policy_the_same_merge_still_posts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    drive_merge(rig)
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


def test_unreadable_detector_text_does_not_post_before_the_blind_hours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(
        monkeypatch,
        tmp_path,
        settings=_settings(tmp_path),
        reader_error="ledger projection down",
    )
    result = drive_merge(rig)
    assert result.verdict is not None
    assert result.verdict.verdict.value == "detector_unreadable"
    assert rig.publisher.on(SLACK_SUFFIX) == []


def test_the_policy_is_read_from_the_settings_store_before_the_bootstrap_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The store holds merge_without_override as a page; the bootstrap default says quiet. The store wins."""
    import asyncio

    from omnimarket.handlers.alarm_post_policy_source import OVERLAY_ID
    from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore

    paged = _policy()
    paged["kinds"] = [
        {**k, "alarm_class": "page", "action": "read it"}  # type: ignore[dict-item]
        if k["kind"] == "merge_without_override"  # type: ignore[index]
        else k
        for k in paged["kinds"]  # type: ignore[attr-defined]
    ]
    store = InMemoryOverlayStore()
    asyncio.run(
        store.write_version(
            {
                "overlay_id": OVERLAY_ID,
                "document": paged,
                "loaded_at": "2026-11-28T00:00:00+00:00",
            }
        )
    )
    source = PostPolicySource(
        ModelAlarmPostPolicy.model_validate(_policy()), store=store
    )
    from omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_alarm import (
        HandlerMergeOverrideAlarm,
    )

    rig = make_rig(monkeypatch, tmp_path, settings=_settings(tmp_path))
    handler = HandlerMergeOverrideAlarm(
        event_publisher=rig.publisher,
        database=rig.database,
        reader=rig.reader,
        clock=rig.clock,
        policy_source=source,
    )
    rig.handler = handler
    drive_merge(rig)
    assert source.last_source == "store"
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


def test_an_unreachable_store_falls_back_to_the_bootstrap_default() -> None:
    class Down:
        async def read_current(self, overlay_id: str) -> None:
            raise ConnectionError("store down")

    source = PostPolicySource(
        ModelAlarmPostPolicy.model_validate(_policy()),
        store=Down(),  # type: ignore[arg-type]
    )
    assert source.current() is not None
    assert source.last_source == "bootstrap"
    assert "ConnectionError" in source.last_store_error


def test_no_store_and_no_bootstrap_default_is_ungated() -> None:
    source = PostPolicySource(None, store_factory=lambda: None)
    assert source.current() is None
