# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (OMN-23867): failing controls. Each injected break turns the named test's own assertions red.

The safety and golden tests keep their assertions in ``disk_inputs.scenario_*``; a control patches one break into the
build, runs the same scenario and expects it to fail. The intact build passes the same scenario first, so a red is the
break and not the scenario.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_storage_effect.handlers import entry_remover_local
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    ModelHostStorageRequest,
    ModelHostStorageResult,
)

from .disk_inputs import scenario_golden, scenario_safety, scenario_worktrees

pytestmark = pytest.mark.unit


def _skips_the_root_check(entry: str, roots: tuple[str, ...]) -> Path | None:
    return Path(os.path.realpath(entry)) if os.path.isabs(entry) else None


def _drops_the_typed_result(
    self: HandlerHostStorage, request: ModelHostStorageRequest
) -> ModelHostStorageResult:
    full = _real_handle(self, request)
    return full.model_copy(update={"entry_results": (), "free_bytes_after": None})


_real_handle = HandlerHostStorage.handle


def test_the_intact_build_passes_every_scenario(tmp_path: Path) -> None:
    for index, scenario in enumerate(
        (scenario_safety, scenario_worktrees, scenario_golden)
    ):
        scenario(tmp_path / str(index))


def test_the_safety_scenario_goes_red_when_the_root_check_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        entry_remover_local, "resolve_inside_roots", _skips_the_root_check
    )
    with pytest.raises(AssertionError):
        scenario_safety(tmp_path)


def test_the_safety_scenario_goes_red_when_the_in_use_check_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entry_remover_local, "is_held", lambda *_: False)
    with pytest.raises(AssertionError):
        scenario_safety(tmp_path)


def test_the_worktree_scenario_goes_red_when_the_lease_check_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entry_remover_local, "is_leased", lambda *_: None)
    with pytest.raises(AssertionError):
        scenario_worktrees(tmp_path)


def test_the_golden_scenario_goes_red_when_the_typed_result_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(HandlerHostStorage, "handle", _drops_the_typed_result)
    with pytest.raises(AssertionError):
        scenario_golden(tmp_path)
