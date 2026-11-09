# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane-facing text that tells a lane to run a gh call is refused (OMN-18946).

The real-gh guard refused 101 lane calls in the 24 hours to 2026-11-09T06:46Z (52 gh pr view, 18 gh pr create,
12 gh api GET, 11 gh pr list, 3 gh run list, 3 gh run view, 2 gh pr comment). The detector names each shape and the
mirror that answers it; each refusal has a positive control (the same text naming the mirror is admitted).
"""

from __future__ import annotations

from typing import Literal

import pytest

from omnimarket.models.model_gh_read_scan import (
    ModelGhReadFinding,
    ModelGhReadScanRequest,
    ModelGhReadSurface,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.handlers.handler_gh_read_scan import (
    HandlerGhReadScan,
)

pytestmark = pytest.mark.unit

REFUSED_SHAPES = [
    "gh pr view 9 --repo o/r --json state",
    "run `gh pr checks 9` and read it",
    "gh pr list --repo o/r --state open",
    "gh pr diff 9",
    "gh run view 123 --log-failed",
    "when the job-log cache misses, run gh run view --log",
    "gh run list --branch main",
    "gh repo view o/r --json defaultBranchRef",
    "gh issue view 9",
    "gh search prs --merged",
    'gh api "repos/$R/contents/x?ref=$SHA"',
    "gh api repos/o/r/commits/abc/check-runs?per_page=100",
    "gh api graphql -f query='{ viewer { login } }'",
    'run(["gh", "pr", "view", str(n)])',
    "curl -s https://api.github.com/repos/o/r",
    "gh pr create --base main --title t",
    "gh pr comment 9 --body-file f",
    "gh workflow run x.yml",
    "gh api -X POST repos/o/r/issues -f title=t",
]
ADMITTED = [
    "onex scm pr-checks o/r#9",
    "onex scm job-log o/r --job-id 77",
    "python3 pr_state_local.py --pr o/r#9",
    'git -C "$OMNI_HOME/repo" show origin/main:path',
    "never run gh pr view from a lane; use the watcher state",
    "`gh run view` is refused from a lane, so read onex scm run-view",
    "use onex scm pr-list instead of gh pr list",
    "ONEX_GH_EXACT_HEAD=o/r#9 gh pr view 9 --repo o/r --json headRefOid,state",
    "gh api rate_limit",
    "gh api --help",
    "gh api graphql -f query='mutation { x }'",
    "> gh pr view 9 was refused at 06:39Z",
    "gh pr merge 9 --squash",
]


def scan(
    text: str, kind: Literal["prose", "code"] = "prose"
) -> tuple[ModelGhReadFinding, ...]:
    return (
        HandlerGhReadScan()
        .handle(
            ModelGhReadScanRequest(
                surfaces=(ModelGhReadSurface(path="x.md", text=text, kind=kind),)
            )
        )
        .findings
    )


@pytest.mark.parametrize("line", REFUSED_SHAPES)
def test_each_refused_gh_shape_is_named_with_its_mirror(line: str) -> None:
    found = scan(line)
    assert found, line
    assert all(f.mirror and f.line == 1 and f.path == "x.md" for f in found)


@pytest.mark.parametrize("line", ADMITTED)
def test_the_mirror_a_negation_quoted_history_and_the_exact_head_are_admitted(
    line: str,
) -> None:
    assert not scan(line), line


def test_a_command_fence_is_read_and_a_pasted_output_fence_is_not() -> None:
    fenced = "Steps:\n```bash\ngh run view 5 --log\n```\n"
    pasted = "Steps:\n```text\ngh run view 5 --log\n```\n"
    assert [f.line for f in scan(fenced)] == [3]
    assert not scan(pasted)


def test_a_code_comment_is_not_an_instruction() -> None:
    code = (
        "# gh pr view 9 is refused\nx = 1\nsubprocess.run(['gh', 'pr', 'view', '9'])\n"
    )
    assert [f.line for f in scan(code, "code")] == [3]
