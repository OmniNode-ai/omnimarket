# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The public knowledge base repository is named by its underscore slug (OMN-20837).

The repository was renamed to ``OmniNode-ai/knowledge_base``. Every live
config, script, workflow, node contract, handler and top-level document in this
repository names it that way. Captured fixtures under ``tests/fixtures`` are
dated history and keep the old slug, so they are not scanned.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Spelled in parts so this file never matches the slug scan it implements.
OLD_SLUG = "OmniNode-ai/" + "knowledge" + "-base"
NEW_SLUG = "OmniNode-ai/knowledge_base"
OLD_SLUG_PATTERN = re.compile(re.escape(OLD_SLUG) + r"(?!-internal)")

LIVE_ROOTS = ("src/", "scripts/", ".github/")


def _live_files() -> list[Path]:
    """Tracked top-level documents and every tracked file under the live roots."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md", *LIVE_ROOTS],
        cwd=REPO_ROOT,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    files = []
    for relative in filter(None, listed.split("\0")):
        if "/" in relative and not relative.startswith(LIVE_ROOTS):
            continue
        path = REPO_ROOT / relative
        if path.is_file():
            files.append(path)
    return sorted(files)


def _old_slug_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if OLD_SLUG_PATTERN.search(line)
    ]


def test_no_live_file_names_the_public_kb_by_its_old_slug() -> None:
    scanned = _live_files()
    assert scanned, "the scan read no file; the repository root is wrong"
    offenders = [line for path in scanned for line in _old_slug_lines(path)]
    assert offenders == [], "\n".join(offenders)


def test_the_kb_nodes_name_the_public_kb_by_its_underscore_slug() -> None:
    for relative in (
        "src/omnimarket/nodes/node_kb_adr_publisher/contract.yaml",
        "src/omnimarket/nodes/node_kb_repowise_index_effect/contract.yaml",
    ):
        assert NEW_SLUG in (REPO_ROOT / relative).read_text(encoding="utf-8"), relative


def test_the_scan_finds_a_planted_old_slug(tmp_path: Path) -> None:
    planted = tmp_path / "planted.md"
    planted.write_text(f"see https://github.com/{OLD_SLUG}/pull/1\n", encoding="utf-8")
    internal = tmp_path / "internal.md"
    internal.write_text(f"see {OLD_SLUG}-internal\n", encoding="utf-8")
    assert OLD_SLUG_PATTERN.search(planted.read_text(encoding="utf-8"))
    assert not OLD_SLUG_PATTERN.search(internal.read_text(encoding="utf-8"))
