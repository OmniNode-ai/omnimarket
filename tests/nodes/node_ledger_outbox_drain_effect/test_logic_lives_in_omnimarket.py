# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox drain logic lives in omnimarket and no Python source sits outside src/omnimarket."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
HANDLER = (
    SRC
    / "omnimarket"
    / "nodes"
    / "node_ledger_outbox_drain_effect"
    / "handlers"
    / "handler_outbox_drain.py"
)


def test_the_drain_handler_is_in_the_omnimarket_package() -> None:
    assert HANDLER.is_file()


def test_no_python_source_sits_outside_the_omnimarket_package() -> None:
    package = SRC / "omnimarket"
    outside = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in SRC.rglob("*.py")
        if not path.is_relative_to(package)
    )
    assert outside == []


def test_a_python_file_outside_the_package_would_be_found(tmp_path: Path) -> None:
    """Positive control: the same scan names a stray source file."""
    package = tmp_path / "src" / "omnimarket"
    package.mkdir(parents=True)
    (package / "inside.py").write_text("", encoding="utf-8")
    stray = tmp_path / "src" / "stray.py"
    stray.write_text("", encoding="utf-8")
    outside = sorted(
        path.name
        for path in (tmp_path / "src").rglob("*.py")
        if not path.is_relative_to(package)
    )
    assert outside == ["stray.py"]
