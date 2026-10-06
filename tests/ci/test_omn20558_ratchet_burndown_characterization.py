# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Characterization tests for the OMN-20558 ratchet burn-down.

Written and committed BEFORE any baseline entry was fixed, and proven green on the
unchanged tree (dev ``b181b79a6``). They must pass unchanged after the burn-down:
that is the proof the burn-down changed what each ratchet tolerates and nothing
else.

1. Workflow action refs. Every ``uses:`` line in ``.github/workflows`` resolves to
   the same action at the same commit it ran on before. A floating tag
   (``@v4``) resolves through ``_TAG_RESOLUTION``, the commit each tag pointed at
   on 2026-10-05 (``git ls-remote``, peeled for annotated tags). A 40-hex ref is
   its own resolution. The golden is ``fixtures/omn20558_workflow_action_refs.json``,
   captured at ``b181b79a6``. Pinning a tag to the commit it already resolves to
   keeps this green; pinning it to any other commit, or bumping a major, fails.

2. Transport-mock lint with no baseline. The lint over every committed
   ``test_*.py`` finds zero bare transport mocks, so its frozen baseline grants
   nothing; a planted bare ``AsyncMock`` is refused (positive control).

3. runtime_profiles with no allowlist. ``ValidatorRuntimeProfiles`` with an
   explicitly empty allowlist (no repo or core allowlist consulted) finds every
   contract under ``src`` valid; a planted contract with no runtime_profiles is
   refused (positive control).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_WORKFLOWS: Final[Path] = _REPO_ROOT / ".github" / "workflows"
_GOLDEN: Final[Path] = (
    Path(__file__).resolve().parent / "fixtures" / "omn20558_workflow_action_refs.json"
)

# The commit each floating tag pointed at on 2026-10-05 (git ls-remote, peeled).
_TAG_RESOLUTION: Final[dict[str, str]] = {
    "actions/cache/restore@v4": "0057852bfaa89a56745cba8c7296529d2fc39830",
    "actions/cache/save@v4": "0057852bfaa89a56745cba8c7296529d2fc39830",
    "actions/cache@v5": "caa296126883cff596d87d8935842f9db880ef25",
    "actions/checkout@v4": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/checkout@v6": "d23441a48e516b6c34aea4fa41551a30e30af803",
    "actions/checkout@v7": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/create-github-app-token@v1": "d72941d797fd3113feb6b93fd0dec494b13a2547",
    "actions/dependency-review-action@v4": "2031cfc080254a8a887f58cffee85186f0e49e48",
    "actions/download-artifact@v4": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/github-script@v7": "f28e40c7f34bde8b3046d885e986cb6290c5673b",
    "actions/setup-python@v5": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/setup-python@v6": "ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact@v4": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/upload-artifact@v7": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "astral-sh/setup-uv@v4": "38f3f104447c67c051c4a08e39b64a148898af3a",
    "astral-sh/setup-uv@v5": "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86",
    "astral-sh/setup-uv@v7": "37802adc94f370d6bfd71619e3f0bf239e1f3b78",
    "github/codeql-action/analyze@v4": "2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2",
    "github/codeql-action/autobuild@v4": "2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2",
    "github/codeql-action/init@v4": "2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2",
    "softprops/action-gh-release@v2": "3bb12739c298aeb8a4eeaf626c5b8d85266b0e65",
    "tj-actions/changed-files@v47": "24d32ffd492484c1d75e0c0b894501ddb9d30d62",
}

_USES_RE: Final[re.Pattern[str]] = re.compile(
    r"""^\s*(?:-\s*)?uses:\s*["']?([^"'#\s]+)["']?"""
)


def _resolved_refs(workflow_dir: Path) -> list[list[object]]:
    rows: list[list[object]] = []
    for wf in sorted(workflow_dir.glob("*.y*ml")):
        for lineno, line in enumerate(
            wf.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = _USES_RE.match(line)
            if match is None:
                continue
            ref = match.group(1)
            if "@" in ref:
                action, _, tag = ref.rpartition("@")
            else:
                action, tag = ref, ""
            rows.append([wf.name, lineno, action, _TAG_RESOLUTION.get(ref, tag)])
    return rows


def _ref_diff(
    expected: list[list[object]], actual: list[list[object]]
) -> tuple[list[list[object]], list[list[object]]]:
    """Return (missing, extra) rows, comparing file, action and resolved commit.

    The line number is carried in each row for the message but is not compared: a
    step added above a ``uses:`` line moves it without changing what it runs, and a
    line-keyed golden failed every pull request once ``ci.yml`` gained a step.
    """

    def key(row: list[object]) -> tuple[object, object, object]:
        return (row[0], row[2], row[3])

    expected_count = Counter(key(row) for row in expected)
    actual_count = Counter(key(row) for row in actual)
    missing_keys = expected_count - actual_count
    extra_keys = actual_count - expected_count
    missing = [row for row in expected if key(row) in missing_keys]
    extra = [row for row in actual if key(row) in extra_keys]
    return missing, extra


@pytest.mark.unit
def test_every_workflow_action_ref_resolves_to_its_pre_burndown_commit() -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    expected = golden["refs"]
    # Positive control: the golden is the full set, not a vacuous one.
    assert len(expected) >= 300, f"golden has only {len(expected)} uses lines"
    missing, extra = _ref_diff(expected, _resolved_refs(_WORKFLOWS))
    assert not missing, f"golden refs no longer present: {missing[:10]}"
    assert not extra, f"refs not in the b181b79a6 golden: {extra[:10]}"


@pytest.mark.unit
def test_action_ref_diff_ignores_line_shift_and_catches_a_moved_pin() -> None:
    """Positive controls for the comparison: a shifted line is no mismatch."""
    golden = [["ci.yml", 10, "actions/checkout", "a" * 40]]
    shifted = [["ci.yml", 18, "actions/checkout", "a" * 40]]
    assert _ref_diff(golden, shifted) == ([], [])
    moved = [["ci.yml", 10, "actions/checkout", "b" * 40]]
    missing, extra = _ref_diff(golden, moved)
    assert missing == golden
    assert extra == moved
    dropped: list[list[object]] = []
    assert _ref_diff(golden, dropped) == (golden, [])


@pytest.mark.unit
def test_resolution_detects_a_moved_pin(tmp_path: Path) -> None:
    """Positive control: a ref pinned to a different commit is a mismatch."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "a.yml").write_text(
        "jobs:\n  a:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/checkout@" + "0" * 40 + "  # v4\n",
        encoding="utf-8",
    )
    rows = _resolved_refs(wf_dir)
    assert rows[0][3] == _TAG_RESOLUTION["actions/checkout@v4"]
    assert rows[1][3] == "0" * 40
    assert rows[0][3] != rows[1][3]


def _python() -> list[str]:
    return [sys.executable]


def _committed_test_files() -> list[str]:
    """Every ``test_*.py`` under ``src`` and ``tests`` (the hook's file filter)."""
    return sorted(
        str(p.relative_to(_REPO_ROOT))
        for top in ("src", "tests")
        for p in (_REPO_ROOT / top).rglob("test_*.py")
        if "__pycache__" not in p.parts
    )


@pytest.mark.unit
def test_transport_mock_lint_finds_nothing_with_no_baseline() -> None:
    files = _committed_test_files()
    assert len(files) > 500, f"only {len(files)} test files listed"
    result = subprocess.run(
        [*_python(), "-m", "omnibase_core.validators.transport_mock_lint", *files],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_transport_mock_lint_refuses_a_planted_bare_mock(tmp_path: Path) -> None:
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        "from unittest.mock import AsyncMock\n\n\n"
        "def test_x() -> None:\n"
        "    event_bus = AsyncMock()\n"
        "    assert event_bus\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            *_python(),
            "-m",
            "omnibase_core.validators.transport_mock_lint",
            str(planted),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "event_bus" in result.stdout + result.stderr


@pytest.mark.unit
def test_runtime_profiles_valid_with_an_empty_allowlist() -> None:
    from omnibase_core.validation.validator_runtime_profiles import (
        ValidatorRuntimeProfiles,
    )

    result = ValidatorRuntimeProfiles(allowlist=set()).validate(_REPO_ROOT / "src")
    assert result.is_valid, [
        f"{i.file_path}:{i.line_number}: {i.message}" for i in result.issues
    ]


@pytest.mark.unit
def test_runtime_profiles_refuses_a_planted_contract(tmp_path: Path) -> None:
    from omnibase_core.validation.validator_runtime_profiles import (
        ValidatorRuntimeProfiles,
    )

    node = tmp_path / "src" / "pkg" / "nodes" / "node_planted_reducer"
    node.mkdir(parents=True)
    (node / "contract.yaml").write_text(
        "name: node_planted_reducer\n"
        "node_type: REDUCER_GENERIC\n"
        "contract_version: {major: 1, minor: 0, patch: 0}\n"
        "node_version: {major: 1, minor: 0, patch: 0}\n"
        "event_bus:\n  subscribe_topics:\n    - onex.cmd.omnimarket.planted.v1\n",
        encoding="utf-8",
    )
    result = ValidatorRuntimeProfiles(allowlist=set()).validate(tmp_path / "src")
    assert not result.is_valid, (
        "a command-consuming contract with no runtime_profiles must be refused"
    )
