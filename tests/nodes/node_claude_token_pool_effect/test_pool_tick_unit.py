# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude token pool rotation tick's registry module main (OMN-23000).

``run_tick`` is monkeypatched with a fake returning a real
``ModelClaudeTokenRotationResult``; the golden pass shows the host from ``ONEX_HOST_NAME`` and the
heartbeat it writes, the probe writes nothing and never calls the tick, and the error chains record
their exit codes in the heartbeat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationResult,
    ModelRotationMarksSync,
)
from omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_pool_tick import (
    HOST_ENV,
    main,
)

pytestmark = pytest.mark.unit

MODULE = "omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_pool_tick"


def _result(
    *,
    outcome: Literal[
        "kept", "swapped", "unmanaged", "no_usable", "planned", "spread"
    ] = "kept",
    host: str = "h101",
    rank: int = 1,
    marks_state: Literal[
        "applied", "unchanged", "unreadable", "stale", "is_source"
    ] = "applied",
) -> ModelClaudeTokenRotationResult:
    return ModelClaudeTokenRotationResult(
        host=host,
        rank=rank,
        outcome=outcome,
        in_use_state="usable",
        marks_sync=ModelRotationMarksSync(state=marks_state),
    )


def _heartbeat(state_dir: Path) -> dict[str, object]:
    return json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))


def test_golden_host_from_env_writes_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOST_ENV, raising=False)
    monkeypatch.setenv(HOST_ENV, "h101")
    monkeypatch.setattr(f"{MODULE}.run_tick", lambda _host, **_kw: _result())
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir)])
    assert code == 0
    beat = _heartbeat(state_dir)
    assert beat["exit_code"] == 0
    assert beat["result"]["status"] == "kept"


def test_probe_writes_nothing_and_never_calls_run_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(*_args, **_kwargs) -> ModelClaudeTokenRotationResult:
        raise AssertionError("run_tick must not run on --probe")

    monkeypatch.setattr(f"{MODULE}.run_tick", _explode)
    monkeypatch.setenv(HOST_ENV, "h101")
    state_dir = tmp_path / "state"
    code = main(["--probe", "--state-dir", str(state_dir)])
    assert code == 0
    assert not (state_dir / "last-run.json").exists()


def test_no_host_and_no_env_is_host_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOST_ENV, raising=False)
    monkeypatch.setattr(
        f"{MODULE}.run_tick",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no host")),
    )
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir)])
    assert code == 3
    beat = _heartbeat(state_dir)
    assert beat["exit_code"] == 3
    assert beat["result"]["status"] == "host_missing"


def test_run_tick_node_overlay_error_is_overlay_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_ENV, "h101")

    def _raise(*_args, **_kwargs) -> ModelClaudeTokenRotationResult:
        raise NodeOverlayError("the overlay file cannot be found under its roots")

    monkeypatch.setattr(f"{MODULE}.run_tick", _raise)
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir)])
    assert code == 3
    beat = _heartbeat(state_dir)
    assert beat["exit_code"] == 3
    assert beat["result"]["status"] == "overlay_refused"


def test_stale_marks_sync_returns_exit_empty_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_ENV, "h101")
    monkeypatch.setattr(
        f"{MODULE}.run_tick", lambda _host, **_kw: _result(marks_state="stale")
    )
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir)])
    assert code == 3, "a stale sync is the INDETERMINATE exit, not 0"
    assert _heartbeat(state_dir)["exit_code"] == 3
