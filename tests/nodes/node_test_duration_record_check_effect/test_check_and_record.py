# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A change that adds a test file must record it: the check refuses, the record command fixes it (OMN-23666)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_test_duration_record_check_effect.adapters.policy_file import (
    load_policy,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.timer_in_memory import (
    TimerInMemory,
)
from omnimarket.nodes.node_test_duration_record_check_effect.handlers.handler_test_duration_record_check import (
    HandlerTestDurationRecordCheck,
)
from omnimarket.nodes.node_test_duration_record_check_effect.models.model_test_duration_record_check import (
    EnumTestDurationRecordOperation,
    ModelTestDurationRecordCheckRequest,
    ModelTestDurationRecordCheckResult,
)

pytestmark = pytest.mark.unit

NEW_FILE = "tests/unit/test_new_thing.py"
OLD_FILE = "tests/unit/test_old_thing.py"


def _checkout(tmp_path: Path, record: dict[str, float], files: tuple[str, ...]) -> Path:
    policy = load_policy()
    (tmp_path / policy.record_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / policy.record_path).write_text(json.dumps(record), encoding="utf-8")
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_x():\n    pass\n", encoding="utf-8")
    return tmp_path


def _check(root: Path, added: tuple[str, ...]) -> ModelTestDurationRecordCheckResult:
    return HandlerTestDurationRecordCheck().handle(
        ModelTestDurationRecordCheckRequest(repo_root=str(root), added_files=added)
    )


def _record(
    root: Path, added: tuple[str, ...], seconds: dict[str, float]
) -> ModelTestDurationRecordCheckResult:
    return HandlerTestDurationRecordCheck(timer=TimerInMemory(seconds)).handle(
        ModelTestDurationRecordCheckRequest(
            repo_root=str(root),
            added_files=added,
            operation=EnumTestDurationRecordOperation.RECORD,
        )
    )


def test_golden_an_added_test_file_that_is_recorded_is_admitted(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {NEW_FILE: 1.5}, (NEW_FILE,))
    result = _check(root, (NEW_FILE,))
    assert result.admitted is True
    assert result.exit_code == 0
    assert result.added_test_files == 1
    assert result.unrecorded == ()
    assert "1 added test file(s), all recorded" in result.output_lines[0]


def test_failing_control_added_file_without_entry_is_refused_with_the_command(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path, {OLD_FILE: 2.0}, (NEW_FILE,))
    result = _check(root, (NEW_FILE,))
    assert result.admitted is False
    assert result.exit_code == 1
    assert result.unrecorded == (NEW_FILE,)
    text = "\n".join(result.output_lines)
    assert (
        f"{NEW_FILE} is a new test file and is not in config/test_file_durations.json"
        in text
    )
    assert f"--record {NEW_FILE}" in result.output_lines[-1]
    assert "runtime_test_duration_record_check" in result.output_lines[-1]


def test_the_refusal_names_every_unrecorded_file_in_one_command(tmp_path: Path) -> None:
    second = "tests/nodes/test_other_new.py"
    root = _checkout(tmp_path, {}, (NEW_FILE, second))
    result = _check(root, (NEW_FILE, second))
    assert result.unrecorded == tuple(sorted((NEW_FILE, second)))
    assert result.output_lines[-1].endswith(
        f"--record {' '.join(sorted((NEW_FILE, second)))}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/conftest.py",
        "tests/unit/helpers.py",
        "scripts/test_not_under_tests.py",
        "tests/.hidden/test_in_a_hidden_dir.py",
        "tests/build/test_in_a_build_dir.py",
        "tests/unit/test_data.json",
    ],
)
def test_a_file_pytest_does_not_collect_needs_no_entry(
    tmp_path: Path, path: str
) -> None:
    root = _checkout(tmp_path, {}, (path,))
    result = _check(root, (path,))
    assert result.admitted is True
    assert result.added_test_files == 0


def test_a_file_the_change_added_and_then_removed_needs_no_entry(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path, {}, ())
    assert _check(root, (NEW_FILE,)).admitted is True


def test_an_unreadable_record_is_a_typed_refusal(tmp_path: Path) -> None:
    result = _check(tmp_path, (NEW_FILE,))
    assert result.admitted is False
    assert result.exit_code == 2
    assert "cannot read config/test_file_durations.json" in result.output_lines[0]


def test_a_record_that_is_not_an_object_is_a_typed_refusal(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {}, (NEW_FILE,))
    (root / load_policy().record_path).write_text("[1, 2]", encoding="utf-8")
    assert _check(root, (NEW_FILE,)).exit_code == 2


def test_recording_a_refused_file_makes_the_next_check_admit(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {OLD_FILE: 2.0}, (NEW_FILE,))
    assert _check(root, (NEW_FILE,)).exit_code == 1
    recorded = _record(root, (NEW_FILE,), {NEW_FILE: 3.14159})
    assert recorded.admitted is True
    assert recorded.recorded == (NEW_FILE,)
    record = json.loads((root / load_policy().record_path).read_text(encoding="utf-8"))
    assert record == {OLD_FILE: 2.0, NEW_FILE: 3.14}
    assert _check(root, (NEW_FILE,)).exit_code == 0


def test_injected_break_a_record_that_writes_nothing_leaves_the_check_red(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path, {}, (NEW_FILE,))
    before = (root / load_policy().record_path).read_bytes()
    broken = HandlerTestDurationRecordCheck().handle(
        ModelTestDurationRecordCheckRequest(
            repo_root=str(root),
            added_files=(NEW_FILE,),
            operation=EnumTestDurationRecordOperation.RECORD,
        )
    )
    assert broken.admitted is False
    assert "no timer composed" in broken.output_lines[0]
    assert (root / load_policy().record_path).read_bytes() == before
    assert _check(root, (NEW_FILE,)).exit_code == 1


def test_a_file_that_cannot_be_timed_writes_nothing(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {OLD_FILE: 2.0}, (NEW_FILE, "tests/unit/test_b.py"))
    before = (root / load_policy().record_path).read_bytes()
    result = _record(
        root, (NEW_FILE, "tests/unit/test_b.py"), {"tests/unit/test_b.py": 1.0}
    )
    assert result.admitted is False
    assert result.exit_code == 1
    assert f"could not time {NEW_FILE}" in result.output_lines[0]
    assert (root / load_policy().record_path).read_bytes() == before


def test_a_fast_file_is_recorded_at_the_policy_floor(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {}, (NEW_FILE,))
    _record(root, (NEW_FILE,), {NEW_FILE: 0.0001})
    record = json.loads((root / load_policy().record_path).read_text(encoding="utf-8"))
    assert record[NEW_FILE] == load_policy().min_seconds


def test_recording_nothing_is_a_refusal(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {}, ("tests/unit/conftest.py",))
    result = _record(root, ("tests/unit/conftest.py",), {})
    assert result.admitted is False
    assert "no collectable test file named" in result.output_lines[0]


def test_the_request_refuses_an_unknown_field() -> None:
    with pytest.raises(ValueError, match="extra"):
        ModelTestDurationRecordCheckRequest.model_validate(
            {"repo_root": "x", "unexpected": True}
        )
