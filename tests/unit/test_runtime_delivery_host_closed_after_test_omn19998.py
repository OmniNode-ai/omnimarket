# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24114: a test's RuntimeDeliveryHost is closed when the test ends.

A projection writer starts a daemon loop thread on its first delivery and
nothing ends it; the leftover threads reach tests that patch time or asyncio
process-wide. The autouse fixture in ``tests/conftest.py`` closes every host a
test constructs, and these two tests hold that guarantee: the first builds a
host and delivers one event (starting its loop thread) without closing it, and
the second asserts the fixture closed it when the first test ended.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from omnimarket.handlers.runtime_delivery_host import RuntimeDeliveryHost
from omnimarket.models.model_message_meta import MessageMeta

pytestmark = pytest.mark.unit


class _ProbeProjection:
    """A tiny projection: records nothing, reports success, closes nothing."""

    async def project_runtime_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True

    async def close_runtime_resources(self) -> None:
        return None


def _thread_alive(name: str) -> bool:
    return any(t.name == name and t.is_alive() for t in threading.enumerate())


def test_a_host_a_test_makes_starts_a_loop_thread() -> None:
    """Delivering one event starts the host's daemon loop thread."""
    host = RuntimeDeliveryHost(_ProbeProjection, name="omn24114-probe")
    meta = MessageMeta(topic="t", partition=0, offset=0, fallback_id="x")
    assert host.deliver("t", {}, meta) is True
    assert _thread_alive("omn24114-probe-loop")


def test_the_host_the_previous_test_made_was_closed_when_it_ended() -> None:
    """Depends on the previous test and on the autouse fixture in tests/conftest.py; red when the fixture is removed."""
    assert not _thread_alive("omn24114-probe-loop")
