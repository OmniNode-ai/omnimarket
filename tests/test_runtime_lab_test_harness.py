# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host timer's entry point of the central test harness (OMN-22478).

Golden: one timer run executes both cases on a lab host, stores a WORKING reading where the floor reads it and writes
the timer's evidence. Error: a broken input that passes, a missing overlay and a missing bus each end the run with the
exit code the unit's evidence records.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.models.lab_test_harness import ModelLabTestHarnessOverlay
from omnimarket.nodes.node_lab_test_harness_effect import (
    runtime_lab_test_harness as runtime,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers import LabWorkCaseSender
from tests.helpers.lab_test_harness_fakes import (
    LiveLabBus,
    make_origin,
    overlay_document,
)

pytestmark = pytest.mark.unit

EFFECT_NODE = "node_lab_test_harness_effect"


def _install_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sha: str, **kw: Any
) -> Path:
    result_path = tmp_path / "state" / "readings" / "central-test-harness.drill.json"
    root = tmp_path / "overlays"
    (root / EFFECT_NODE).mkdir(parents=True)
    (root / EFFECT_NODE / "overlay.yaml").write_text(
        yaml.safe_dump(overlay_document(sha, result_path, **kw)), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return result_path


def _serve_on(monkeypatch: pytest.MonkeyPatch, live: LiveLabBus) -> None:
    def opener(
        overlay: ModelLabTestHarnessOverlay, workspace_root: Path | None = None
    ) -> contextlib.AbstractAsyncContextManager[LabWorkCaseSender]:
        return live.opener(overlay)

    monkeypatch.setattr(runtime, "open_lab_work_runner", opener)


def _evidence(state_dir: Path) -> dict[str, Any]:
    return json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))


def test_golden_a_timer_run_stores_a_working_reading_and_writes_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url_base, sha = make_origin(tmp_path)
    result_path = _install_overlay(tmp_path, monkeypatch, sha)
    _serve_on(monkeypatch, LiveLabBus(url_base, tmp_path / "work"))
    state_dir = tmp_path / "evidence"
    code = runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)])
    assert code == 0
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    assert stored["state"] == "WORKING"
    evidence = _evidence(state_dir)
    assert evidence["reading_state"] == "WORKING"
    assert evidence["exit_code"] == 0
    assert evidence["run_id"].startswith("schedule-")


def test_error_a_broken_input_that_passes_turns_the_run_and_its_evidence_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url_base, sha = make_origin(tmp_path)
    result_path = _install_overlay(tmp_path, monkeypatch, sha, error="passes.py")
    _serve_on(monkeypatch, LiveLabBus(url_base, tmp_path / "work"))
    state_dir = tmp_path / "evidence"
    code = runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)])
    assert code == 1
    assert json.loads(result_path.read_text(encoding="utf-8"))["state"] == "NOT WORKING"
    assert _evidence(state_dir)["reading_state"] == "NOT WORKING"
    assert _evidence(state_dir)["exit_code"] == 1


def test_error_without_the_overlay_the_run_is_refused_and_the_evidence_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "no-overlays"
    empty.mkdir()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(empty))
    state_dir = tmp_path / "evidence"
    code = runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)])
    assert code == 65
    assert _evidence(state_dir) == {
        **_evidence(state_dir),
        "reading_state": "none",
        "exit_code": 65,
    }


def test_error_a_bus_that_is_unavailable_ends_the_run_with_69(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _url_base, sha = make_origin(tmp_path)
    _install_overlay(tmp_path, monkeypatch, sha)

    class _Down:
        async def __aenter__(self) -> LabWorkCaseSender:
            raise LabRunBusError("no bus in this test")

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    def down(
        overlay: ModelLabTestHarnessOverlay, workspace_root: Path | None = None
    ) -> _Down:
        return _Down()

    monkeypatch.setattr(runtime, "open_lab_work_runner", down)
    state_dir = tmp_path / "evidence"
    code = runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)])
    assert code == 69
    assert _evidence(state_dir)["exit_code"] == 69
