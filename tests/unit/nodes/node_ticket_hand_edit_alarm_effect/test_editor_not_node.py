# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: a revision by an actor other than the overlay's node identity is named."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HAND_EDITED_BODY,
    NODE_ACTOR,
    SETTINGS,
    SLACK_SUFFIX,
    WRITTEN_BODY,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit


def test_editor_not_node_names_the_actor_even_when_the_content_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(
        [issue(WRITTEN_BODY)], [revision(20, NODE_ACTOR), revision(10, HAND)]
    )
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    rig.sweep()

    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert event["kind"] == "ticket_hand_edit"
    assert event["editors"] == [HAND]
    assert HAND in str(rig.publisher.on(SLACK_SUFFIX)[0]["text"])


def test_editor_not_node_ignores_revisions_the_node_made_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(
        [issue(WRITTEN_BODY)], [revision(10, NODE_ACTOR), revision(8, NODE_ACTOR)]
    )
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    rig.sweep()

    assert rig.publisher.calls == []


def test_editor_not_node_ignores_revisions_before_the_node_last_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(
        [issue(WRITTEN_BODY)], [revision(30, HAND), revision(20, NODE_ACTOR)]
    )
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    rig.sweep()

    assert rig.publisher.calls == []


def test_editor_not_node_without_an_identity_reads_no_history_of_a_matching_ticket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(WRITTEN_BODY)], [revision(10, HAND)])
    settings = {k: v for k, v in SETTINGS.items() if k != "node_actor_id"}
    rig = make_rig(monkeypatch, tmp_path, source=source, settings=settings)
    rig.handler.handle(written())

    rig.sweep()

    assert source.history_calls == 0
    assert rig.publisher.calls == []


def test_editor_not_node_lists_every_editor_of_a_changed_ticket_but_the_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(
        [issue(HAND_EDITED_BODY)],
        [revision(10, HAND, NODE_ACTOR), revision(8, "example-second-actor")],
    )
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    rig.sweep()

    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert event["editors"] == [HAND, "example-second-actor"]
