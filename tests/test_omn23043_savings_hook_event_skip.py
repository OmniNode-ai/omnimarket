# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A hook event the savings projection ignores costs no database setup (OMN-23043).

``onex.evt.omniclaude.hook-event.v1`` carries every Claude Code hook event, and the
savings projection acts on PostCompact only. The runtime writer built a runner and
opened and attested the workload bindings for every delivery, so a consumer that
should pass an ignored event over in microseconds took about half a second each and
fell behind the topic. Golden chain: a PostCompact event still reaches the runner.
Error chain: a malformed PostCompact event still reaches the runner (and so its DLQ
route) rather than being passed over.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from omnimarket.nodes.node_projection_savings.handlers.handler_runtime_writer import (
    SavingsProjectionWriter,
)

pytestmark = pytest.mark.unit

HOOK_TOPIC = "onex.evt.omniclaude.hook-event.v1"


class _RecordingRunner:
    """Stands in for the runner: counts constructions, which open the bindings."""

    constructed: ClassVar[int] = 0
    seen: ClassVar[list[dict[str, Any]]] = []

    def __init__(self) -> None:
        type(self).constructed += 1

    async def project_runtime_event(
        self, topic: str, data: dict[str, Any], meta: object
    ) -> bool:
        type(self).seen.append(data)
        return True

    async def close_runtime_resources(self) -> None:
        return None


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> SavingsProjectionWriter:
    _RecordingRunner.constructed = 0
    _RecordingRunner.seen = []
    monkeypatch.setattr(SavingsProjectionWriter, "runner_type", _RecordingRunner)
    return SavingsProjectionWriter()


def _request(topic: str, offset: int, **payload: Any) -> dict[str, Any]:
    return {"_topic": topic, "_partition": 0, "_offset": offset, **payload}


@pytest.mark.parametrize(
    "hook_event_name",
    ["PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop", None],
)
def test_ignored_hook_event_opens_no_bindings(
    writer: SavingsProjectionWriter, hook_event_name: str | None
) -> None:
    payload: dict[str, Any] = {"event_id": "e-1"}
    if hook_event_name is not None:
        payload["hook_event_name"] = hook_event_name
    out = writer.handle(_request(HOOK_TOPIC, 1, **payload))
    assert out == {"projected": True}
    assert _RecordingRunner.constructed == 0


def test_post_compact_hook_event_reaches_the_runner(
    writer: SavingsProjectionWriter,
) -> None:
    out = writer.handle(
        _request(HOOK_TOPIC, 2, event_id="e-2", hook_event_name="PostCompact")
    )
    assert out == {"projected": True}
    assert _RecordingRunner.constructed == 1
    assert _RecordingRunner.seen[0]["hook_event_name"] == "PostCompact"


def test_malformed_post_compact_event_still_reaches_the_runner(
    writer: SavingsProjectionWriter,
) -> None:
    writer.handle(
        _request(
            HOOK_TOPIC,
            3,
            event_id="e-3",
            hook_event_name="PostCompact",
            emitted_at=12345,
        )
    )
    assert _RecordingRunner.constructed == 1


def test_other_topics_are_never_skipped(writer: SavingsProjectionWriter) -> None:
    writer.handle(
        _request(
            "onex.evt.omnimarket.delegate-skill-completed.v1",
            4,
            hook_event_name="PreToolUse",
        )
    )
    assert _RecordingRunner.constructed == 1


def test_a_skipped_event_without_source_coordinates_fails_as_the_base_does(
    writer: SavingsProjectionWriter,
) -> None:
    """Error chain: an ignored event lacking its coordinates is not silently passed over."""
    with pytest.raises(KeyError, match="_partition"):
        writer.handle({"_topic": HOOK_TOPIC, "_offset": 5, "hook_event_name": "Stop"})
    assert _RecordingRunner.constructed == 0
