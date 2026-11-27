"""The runner's PATH keeps the caller's entries ahead of its fallback directories.

The fallback directories exist so a non-login ssh shell still finds user-level
tools; they must never shadow a tool the caller put on PATH deliberately.
"""

from __future__ import annotations

import os

import pytest

from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    local_shell_work_runner as runner,
)

pytestmark = pytest.mark.unit


def test_callers_path_entries_come_before_the_fallback_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/caller/first:/caller/second")
    parts = runner._env()["PATH"].split(os.pathsep)
    fallbacks = [os.path.expanduser(p) for p in runner._EXTRA_PATH]
    assert parts[:2] == ["/caller/first", "/caller/second"]
    assert parts[2:] == fallbacks


def test_fallback_directories_still_reach_a_bare_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    parts = runner._env()["PATH"].split(os.pathsep)
    assert os.path.expanduser("~/.local/bin") in parts
