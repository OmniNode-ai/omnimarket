# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The build history is kept under the runtime's declared writable state root, never under the registry root.

The dev runtime container mounts the registry root (OMNI_HOME=/app) read-only and declares its writable state root in
ONEX_STATE_DIR / ONEX_STATE_ROOT (/app/data/.onex_state). The first build of the dev lane failed with
``PermissionError: [Errno 13] Permission denied: '/app/.onex_state'`` because the history path was derived from OMNI_HOME.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from omnimarket.config.state_root import OnexStateRootUnconfiguredError
from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.bus_sink import (
    BusRegressionSink,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    OMNI_HOME_ENV,
    REPLAY_ENV,
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumBuildChecksStatus,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_build_delegation_checks.overlay import FILE_ENV
from tests.nodes.node_build_delegation_checks.test_bus_wiring import _git, _recorded


@pytest.fixture
def read_only_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """A registry root the process cannot write (the runtime's /app) and a separate writable state root."""
    home = tmp_path / "app"
    home.mkdir()
    clone = home / "omnimarket"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "first (#1)")
    green = _git(clone, "rev-parse", "HEAD")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "second (#2)")
    red = _git(clone, "rev-parse", "HEAD")
    replay = tmp_path / "recorded.json"
    _recorded(replay, green, red)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"checks": ["C14", "C29"], "owner": "delegation-owner"}),
        encoding="utf-8",
    )
    state = tmp_path / "data" / ".onex_state"
    state.mkdir(parents=True)
    monkeypatch.setenv(OMNI_HOME_ENV, str(home))
    monkeypatch.setenv(REPLAY_ENV, str(replay))
    monkeypatch.setenv(FILE_ENV, str(overlay))
    monkeypatch.setenv("ONEX_STATE_DIR", str(state))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)

    async def swallow(
        self: BusRegressionSink, topic: str, key: str, value: bytes
    ) -> None:
        return None

    monkeypatch.setattr(BusRegressionSink, "_send", swallow)
    home.chmod(stat.S_IRUSR | stat.S_IXUSR)
    # only the registry root itself is closed to new entries; the clone inside stays readable for git
    try:
        yield home, state
    finally:
        home.chmod(stat.S_IRWXU)


def _shas(home: Path) -> tuple[str, str]:
    clone = home / "omnimarket"
    log = _git(clone, "log", "--format=%H", "--reverse").splitlines()
    return log[0], log[1]


def test_the_history_is_written_under_the_declared_state_root_with_the_registry_root_read_only(
    read_only_home: tuple[Path, Path],
) -> None:
    home, state = read_only_home
    assert not os.access(home, os.W_OK), (
        "the probe needs a registry root the process cannot write"
    )
    green, red = _shas(home)

    handler = HandlerBuildDelegationChecks()
    first = handler.handle(
        ModelRebuildCompletedObserved(git_sha=green, runtime_lane="dev")
    )
    second = handler.handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert first.status != EnumBuildChecksStatus.ALREADY_RUN
    assert second.status != EnumBuildChecksStatus.ALREADY_RUN
    history = state / ModelBuildDelegationChecksOverlay().history_relpath
    assert history.is_file()
    assert [b["build_id"] for b in json.loads(history.read_text(encoding="utf-8"))] == [
        green,
        red,
    ]
    assert not (home / ".onex_state").exists()


def test_a_replayed_build_still_runs_nothing_from_the_state_root_history(
    read_only_home: tuple[Path, Path],
) -> None:
    home, _state = read_only_home
    _green, red = _shas(home)

    handler = HandlerBuildDelegationChecks()
    handler.handle(ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev"))
    replayed = HandlerBuildDelegationChecks().handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert replayed.status == EnumBuildChecksStatus.ALREADY_RUN


def test_no_declared_state_root_refuses_instead_of_choosing_a_path(
    read_only_home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state = read_only_home
    _green, red = _shas(home)
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)

    with pytest.raises(OnexStateRootUnconfiguredError):
        HandlerBuildDelegationChecks().handle(
            ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
        )
    assert not (home / ".onex_state").exists()


@pytest.mark.parametrize(
    "bad", ["/etc/builds.json", "../builds.json", "a/../../b.json"]
)
def test_the_history_setting_is_a_path_inside_the_state_root(bad: str) -> None:
    with pytest.raises(ValueError, match="history_relpath"):
        ModelBuildDelegationChecksOverlay(history_relpath=bad)
