# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No skill, script or brief template in this repository names a direct gh call to a lane (OMN-18946).

Floor row ``hook-refusal:lane-github-read-real-gh``: 110 occurrences in the 24 hours to 2026-11-09T06:51Z, each a lane
that ran ``gh`` directly after a brief, a skill or a prompt template told it to. This is the whole-tree control for the
surfaces this repository owns: the skills under ``plugins/`` and the worker prompt templates. It judges every line, not
only an addition, and holds no allowlist and no baseline: a finding is moved to the mirror (``onex scm``,
``pr_state_local.py``, the PR watcher state), never listed here. The runtime nodes that are the readers themselves
(the GitHub gateway, the PR poller) run under the runtime's identity and are not lane text; they are not scanned.

Each refusal has a positive control (the surface naming the mirror passes) and the scan has a named injected break.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_gh_read_scan_compute.handlers.handler_gh_read_scan import (
    HandlerGhReadScan,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.models.model_gh_read_scan import (
    ModelGhReadScanRequest,
    ModelGhReadSurface,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
# Lane-facing text this repository owns: every skill file, and the prompt templates the dispatch worker hands a lane.
LANE_TEXT_GLOBS = (
    "plugins/**/skills/**/*.md",
    "plugins/**/skills/**/*.py",
    "plugins/**/skills/**/*.sh",
    "src/omnimarket/nodes/node_dispatch_worker/handlers/handler_dispatch_worker.py",
)
CODE_SUFFIXES = {".py", ".sh"}


def surfaces() -> tuple[ModelGhReadSurface, ...]:
    found: dict[Path, ModelGhReadSurface] = {}
    for pattern in LANE_TEXT_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file() and "__pycache__" not in path.parts:
                found[path] = ModelGhReadSurface(
                    path=path.relative_to(ROOT).as_posix(),
                    text=path.read_text(encoding="utf-8"),
                    kind="code" if path.suffix in CODE_SUFFIXES else "prose",
                )
    return tuple(found.values())


def scan(items: tuple[ModelGhReadSurface, ...]) -> list[str]:
    result = HandlerGhReadScan().handle(ModelGhReadScanRequest(surfaces=items))
    return [f"{f.path}:{f.line}: `{f.command}` -> {f.mirror}" for f in result.findings]


def test_the_scan_reads_the_surfaces_it_claims_to_read() -> None:
    names = {s.path for s in surfaces()}
    assert any(n.startswith("plugins/") and n.endswith("SKILL.md") for n in names)
    assert (
        "src/omnimarket/nodes/node_dispatch_worker/handlers/handler_dispatch_worker.py"
        in names
    )


def test_no_lane_facing_text_names_a_direct_gh_call() -> None:
    assert scan(surfaces()) == []


def test_named_break_a_skill_that_tells_a_lane_to_run_gh_run_view_is_refused() -> None:
    skill = next(s for s in surfaces() if s.path.endswith("SKILL.md"))
    broken = skill.model_copy(
        update={
            "text": skill.text
            + "\nWhen the job-log cache misses, run `gh run view --log` on the run.\n"
        }
    )
    found = scan((broken,))
    assert len(found) == 1
    assert "onex scm run-view" in found[0]
    # Positive control: the same surface with the mirror named instead is clean.
    fixed = skill.model_copy(
        update={
            "text": skill.text
            + "\nWhen the job-log cache misses, say so; read `onex scm job-log <owner>/<repo> --job-id <id>`.\n"
        }
    )
    assert scan((fixed,)) == []


def test_named_break_a_worker_template_naming_gh_pr_checks_is_refused() -> None:
    template = next(
        s for s in surfaces() if s.path.endswith("handler_dispatch_worker.py")
    )
    broken = template.model_copy(
        update={"text": template.text + "\n1. gh pr checks {target_pr}\n"}
    )
    assert len(scan((broken,))) == 1
