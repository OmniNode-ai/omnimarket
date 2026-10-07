# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/check_action_pins.py (OMN-14762 / F-19-A).

The check requires every third-party ``uses:`` be SHA-pinned. OMN-20558 burned
the frozen baseline down to empty and deleted it, so ANY unpinned, non-exempt
ref fails. Local ``./`` actions and first-party ``@main`` reusables are exempt.
RED->GREEN proven against synthesized workflow dirs; the real repo passes with
no baseline at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

from check_action_pins import is_exempt, is_pinned, main, unpinned_refs  # noqa: E402

_SHA = "1234567890abcdef1234567890abcdef12345678"  # 40 hex


def _make_workflow(tmp_path: Path, body: str) -> Path:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_text(body, encoding="utf-8")
    return tmp_path


class TestPinPredicates:
    def test_sha_is_pinned(self) -> None:
        assert is_pinned(f"actions/checkout@{_SHA}")

    def test_tag_is_not_pinned(self) -> None:
        assert not is_pinned("actions/checkout@v4")
        assert not is_pinned("actions/checkout@main")
        assert not is_pinned("astral-sh/setup-uv@0.34.0")

    def test_local_action_is_exempt(self) -> None:
        assert is_exempt("./.github/actions/foo")
        assert not is_exempt("actions/checkout@v4")

    def test_first_party_main_reusable_is_exempt(self) -> None:
        # Org policy: first-party reusable workflows / composite actions ride @main.
        assert is_exempt(
            "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml@main"
        )
        assert is_exempt("OmniNode-ai/omniclaude/.github/actions/deploy-gate@main")
        # A first-party ref on a non-main tag is NOT exempt.
        assert not is_exempt("OmniNode-ai/omnibase_core/.github/workflows/x.yml@v1")
        # A third-party @main is NOT exempt.
        assert not is_exempt("third-party/action@main")


class TestPlainCheck:
    def test_unpinned_fails_closed(self, tmp_path: Path) -> None:
        root = _make_workflow(
            tmp_path,
            "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n",
        )
        assert main(["--repo-root", str(root)]) == 1

    def test_one_unpinned_among_pinned_fails(self, tmp_path: Path) -> None:
        root = _make_workflow(
            tmp_path,
            f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{_SHA}\n"
            "      - uses: astral-sh/setup-uv@v7\n",
        )
        assert main(["--repo-root", str(root)]) == 1

    def test_sha_pinned_passes(self, tmp_path: Path) -> None:
        root = _make_workflow(
            tmp_path,
            f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{_SHA}  # v4\n",
        )
        assert main(["--repo-root", str(root)]) == 0

    def test_local_action_passes(self, tmp_path: Path) -> None:
        root = _make_workflow(
            tmp_path,
            "jobs:\n  a:\n    steps:\n      - uses: ./.github/actions/local\n",
        )
        assert main(["--repo-root", str(root)]) == 0

    def test_empty_workflows_fails_closed(self, tmp_path: Path) -> None:
        # Non-vacuity: a workflows dir that parses ZERO `uses:` means the matcher
        # broke; the gate must fail closed rather than pass silently.
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("jobs:\n  a:\n    steps: []\n", encoding="utf-8")
        assert main(["--repo-root", str(tmp_path)]) == 1

    def test_no_baseline_option_exists(self, tmp_path: Path) -> None:
        # The exception mechanism is gone: neither flag is accepted.
        root = _make_workflow(
            tmp_path,
            "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n",
        )
        for flag in (["--baseline", str(tmp_path / "b.txt")], ["--update-baseline"]):
            with pytest.raises(SystemExit):
                main(["--repo-root", str(root), *flag])
        assert unpinned_refs(root / ".github" / "workflows") == {"actions/checkout@v4"}


class TestRealRepo:
    def test_real_repo_has_no_unpinned_action(self) -> None:
        assert main(["--repo-root", str(REPO_ROOT)]) == 0
        assert not (REPO_ROOT / "scripts" / "ci" / "action_pin_baseline.txt").exists()
