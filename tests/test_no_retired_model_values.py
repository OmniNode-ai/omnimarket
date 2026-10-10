# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retired lab values cannot return outside immutable historical records."""

from __future__ import annotations

import os
from pathlib import Path

from omnibase_core.validation.hardcoded_model_config.handler import classify_path
from omnibase_core.validation.hardcoded_model_config.runtime_hardcoded_model_config import (
    load_policy,
)

_ROOT = Path(__file__).resolve().parents[1]


def _retired_findings(root: Path) -> list[str]:
    policy = load_policy()
    findings: list[str] = []
    # Walk source files directly: tests scrub inherited git plumbing, and the
    # guard must also work in a transplanted checkout without git metadata.
    generated = {
        ".git",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
    }
    for directory, children, files in os.walk(root):
        children[:] = [child for child in children if child not in generated]
        for filename in files:
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            if classify_path(relative, policy).name == "HISTORICAL":
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                for value in policy.retired_values:
                    if value.casefold() in line.casefold():
                        findings.append(f"{relative}:{number}: {value}")
    return findings


def test_no_retired_model_values() -> None:
    findings = _retired_findings(_ROOT)
    assert not findings, "Retired lab values remain:\n" + "\n".join(findings)


def test_retired_values_are_detected_in_comments_and_fixture_files(
    tmp_path: Path,
) -> None:
    policy = load_policy()
    for relative in ("src/fixture.py", "tests/fixture.yaml", "docs/fixture.md"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(f"# {value}" for value in policy.retired_values), encoding="utf-8"
        )
    findings = _retired_findings(tmp_path)
    assert len(findings) == 3 * len(policy.retired_values)


def test_historical_records_keep_their_retired_values(tmp_path: Path) -> None:
    path = tmp_path / "evidence" / "fixture.yaml"
    path.parent.mkdir()
    path.write_text("\n".join(load_policy().retired_values), encoding="utf-8")
    assert _retired_findings(tmp_path) == []
