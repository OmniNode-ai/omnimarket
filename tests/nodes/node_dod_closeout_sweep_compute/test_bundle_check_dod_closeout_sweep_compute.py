# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's bundle check refuses a bad bundle with a readable error file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.handlers import closeout_bundle_check as check
from omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep import (
    main as sweep_main,
)

GOOD = {
    "candidates": [{"id": "OMN-1"}],
    "pr_state": [],
    "text_state": [],
    "held": ["OMN-2"],
    "waiting": [{"id": "OMN-3", "reason": "PR state unread"}],
    "totals": {
        "candidates": 5,
        "eligible": 4,
        "selected": 1,
        "deferred": 2,
        "held": 1,
        "waiting": 1,
    },
    "held_count": 1,
    "waiting_count": 1,
}


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))


def _run(tmp_path: Path, raw: str, code: int = 0, stderr: str = "") -> tuple[int, str]:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(raw)
    err = tmp_path / "stderr.txt"
    err.write_text(stderr)
    return check.check(bundle, err, code, "2026-11-20T0545Z")


def test_a_good_bundle_passes_through_unchanged(tmp_path: Path) -> None:
    raw = json.dumps(GOOD)
    assert _run(tmp_path, raw + "\n") == (0, raw)
    assert not (tmp_path / "state").exists()


def test_held_that_is_not_an_array_fails_loudly_with_a_readable_file(
    tmp_path: Path,
) -> None:
    code, line = _run(tmp_path, json.dumps({**GOOD, "held": {"0": "OMN-2"}}))
    assert code == 2
    assert line.startswith(
        "BUNDLE_FAILED: bundle.held is object, not an array; error_file="
    )
    path = Path(line.split("error_file=", 1)[1])
    assert (
        path
        == tmp_path / "state/closeout-sweep/errors/2026-11-20T0545Z.bundle-error.txt"
    )
    text = path.read_text()
    assert "- bundle.held is object, not an array" in text
    assert "bundle head:" in text
    assert "a bundle that cannot be read is never a zero" in text


@pytest.mark.parametrize(
    ("raw", "code", "problem"),
    [
        ("not json", 0, "the bundle is not JSON"),
        ("[]", 0, "the bundle is array, not an object"),
        ("", 0, "the bundle command printed nothing"),
        (json.dumps(GOOD), 1, "the bundle command exited 1"),
        (
            json.dumps({**GOOD, "totals": {**GOOD["totals"], "selected": 3}}),
            0,
            "totals.selected is 3",
        ),
        (
            json.dumps({**GOOD, "waiting": ["x"]}),
            0,
            "bundle.waiting is not a list of {id, reason}",
        ),
        (
            json.dumps({**GOOD, "held_count": -1}),
            0,
            "bundle.held_count is number, not a count",
        ),
    ],
)
def test_each_schema_break_is_named(
    tmp_path: Path, raw: str, code: int, problem: str
) -> None:
    got, line = _run(tmp_path, raw, code, stderr="uv: boom")
    assert got == 2
    assert problem in line
    text = Path(line.split("error_file=", 1)[1]).read_text()
    assert problem in text
    assert "uv: boom" in text


def test_a_run_key_that_could_escape_the_directory_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid run key"):
        check.error_path("../x")


def test_the_runtime_command_prints_the_bundle_or_the_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(GOOD))
    assert sweep_main(["check-bundle", "--bundle", str(bundle), "--run", "r1"]) == 0
    assert json.loads(capsys.readouterr().out) == GOOD
    bundle.write_text(json.dumps({**GOOD, "held": "OMN-2"}))
    assert sweep_main(["check-bundle", "--bundle", str(bundle), "--run", "r1"]) == 2
    out = capsys.readouterr()
    assert out.out.startswith(
        "BUNDLE_FAILED: bundle.held is string, not an array; error_file="
    )
    assert (tmp_path / "state/closeout-sweep/errors/r1.bundle-error.txt").is_file()
