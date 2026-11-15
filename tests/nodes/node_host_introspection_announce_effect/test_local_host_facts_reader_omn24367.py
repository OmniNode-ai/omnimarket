# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24367 -- the local facts reader spawns its version probes once per TTL, not once per ten-second beat."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_introspection_announce_effect.protocols import (
    LocalHostFactsReader,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols import (
    local_host_facts_reader as reader_module,
)

pytestmark = pytest.mark.unit


def test_a_reading_is_reused_inside_the_ttl_and_taken_again_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []

    def fake_version(program: str, path: str) -> tuple[str, str] | None:
        probes.append(program)
        return ("1.0.0", "") if program == "git" else None

    monkeypatch.setattr(reader_module, "version_and_commit", fake_version)

    def not_logged_in(_path: str) -> bool:
        return False

    def nothing_installed(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(reader_module, "_claude_logged_in", not_logged_in)
    monkeypatch.setattr(reader_module.shutil, "which", nothing_installed)
    clock = [0.0]
    reader = LocalHostFactsReader(now=lambda: clock[0], ttl_seconds=60.0)

    first = reader.read(["git"])
    spawned = len(probes)
    assert spawned > 0
    assert [s.name for s in first.software] == ["git"]
    clock[0] = 59.0
    assert reader.read(["git"]) is first
    assert len(probes) == spawned  # inside the TTL: no new probe
    clock[0] = 61.0
    reader.read(["git"])
    assert len(probes) == 2 * spawned  # past it: read again
