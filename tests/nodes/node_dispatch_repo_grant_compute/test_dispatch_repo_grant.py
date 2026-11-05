# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch names every repository a brief changes, so the lane is granted them at send."""

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dispatch_repo_grant_compute import (
    NodeDispatchRepoGrantCompute,
)
from omnimarket.nodes.node_dispatch_repo_grant_compute.models.model_dispatch_repo_grant import (
    ModelDispatchRepoGrantRequest,
)

pytestmark = pytest.mark.unit

NODE = NodeDispatchRepoGrantCompute()

# The recorded case: the dispatch granted git in the lane's --repo only, while the brief's own
# CLAIM and worktree put the fix in a second repository, so the lane could not push it.
RECORDED_BRIEF = """\
# Brief: lane fix-runner-7

## 2. Task

The bad default lives in the runner script, not in this repository. Fix it there.

```bash
... | CLAIM | lane=fix-runner-7 | ticket=OMN-1 | repo=omnibase_infra | worktree=omni_worktrees/OMN-1/omnibase_infra | scope=x
```

Open the PR with `onex scm pr-create OmniNode-ai/omnibase_infra --head omn-1-x --base dev`.
"""


def grant(brief: str, repo: str | None = "omnimarket", granted=()):
    return NODE.handle(
        ModelDispatchRepoGrantRequest(brief=brief, repo=repo, granted=tuple(granted))
    )


def test_recorded_case_grants_the_repository_the_fix_belongs_in():
    result = grant(RECORDED_BRIEF)
    assert result.grant == ("OmniNode-ai/omnimarket", "OmniNode-ai/omnibase_infra")
    assert result.ungranted == ("OmniNode-ai/omnibase_infra",)
    assert result.receipt_line == (
        "REMOTE-LANE-REPO-GRANT added OmniNode-ai/omnibase_infra to the dispatch's grant "
        "(the brief changes it: repo=omnibase_infra)"
    )


def test_a_brief_inside_its_grant_adds_nothing():
    brief = "Fix it.\n| repo=omnimarket | worktree=omni_worktrees/OMN-2/omnimarket |\n"
    result = grant(brief)
    assert result.grant == ("OmniNode-ai/omnimarket",)
    assert result.ungranted == ()
    assert result.receipt_line is None


def test_an_explicit_grant_is_kept_and_not_reported_ungranted():
    result = grant(RECORDED_BRIEF, granted=("OmniNode-ai/omnibase_infra",))
    assert result.grant == ("OmniNode-ai/omnimarket", "OmniNode-ai/omnibase_infra")
    assert result.ungranted == ()


@pytest.mark.parametrize(
    ("line", "repo"),
    [
        ("| repo=omniclaude |", "OmniNode-ai/omniclaude"),
        ("| fix-repo=omniclaude |", "OmniNode-ai/omniclaude"),
        ("worktree=omni_worktrees/OMN-9/omniclaude", "OmniNode-ai/omniclaude"),
        (
            "cd $HOME/x/omni_worktrees/OMN-9/omniclaude/ && make",
            "OmniNode-ai/omniclaude",
        ),
        ("run with --repo omniclaude now", "OmniNode-ai/omniclaude"),
        ("run with --repo=omniclaude now", "OmniNode-ai/omniclaude"),
        ("run with --fix-repo omniclaude now", "OmniNode-ai/omniclaude"),
        ("Repo: omniclaude", "OmniNode-ai/omniclaude"),
        ("- Target repo: omniclaude", "OmniNode-ai/omniclaude"),
        ("Repository: OmniNode-ai/omniclaude.", "OmniNode-ai/omniclaude"),
        (
            "onex scm pr-create OmniNode-ai/omniclaude --head x",
            "OmniNode-ai/omniclaude",
        ),
        ("| pr=OmniNode-ai/omniclaude#12 |", "OmniNode-ai/omniclaude"),
        ("a GitHub checkout of OmniNode-ai/omniclaude.", "OmniNode-ai/omniclaude"),
        ("| repo=Other-org/tool |", "Other-org/tool"),
    ],
)
def test_change_signals(line, repo):
    assert grant(line, repo=None).grant == (repo,)


@pytest.mark.parametrize(
    "line",
    [
        "| repo=<repo> |",
        "worktree=omni_worktrees/<ticket>/<repo>",
        "$OMNI_HOME/../x/omni_worktrees/<ticket>/omniclaude/",
        "--repo <repo>",
        "Repo: <name>",
        "> | repo=omniclaude | (a quoted ticket text, not this lane's work)",
        "Gate: OmniNode-ai/omniclaude validate-plugin.yml",
        "read origin/dev of omniclaude with git -C",
    ],
)
def test_placeholders_quotes_and_mentions_grant_nothing(line):
    assert grant(line, repo=None).grant == ()


def test_same_repository_spelled_twice_is_granted_once():
    brief = "| repo=OmniClaude |\n--repo omniclaude\n| pr=omninode-ai/omniclaude#3 |"
    assert grant(brief, repo=None).grant == ("OmniNode-ai/OmniClaude",)


def test_decision_is_deterministic():
    brief = RECORDED_BRIEF + "\n| repo=omniclaude |\n"
    assert grant(brief) == grant(brief)
    assert grant(brief).grant == (
        "OmniNode-ai/omnimarket",
        "OmniNode-ai/omnibase_infra",
        "OmniNode-ai/omniclaude",
    )


def test_contract_declares_the_bus_topics():
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_dispatch_repo_grant_compute/contract.yaml"
        ).read_text()
    )
    command = "onex.cmd.omnimarket.dispatch-repo-grant-requested.v1"
    terminal = "onex.evt.omnimarket.dispatch-repo-grant-completed.v1"
    assert contract["event_bus"] == {
        "subscribe_topics": [command],
        "publish_topics": [terminal],
    }
    assert contract["terminal_event"] == terminal
    assert contract["descriptor"]["purity"] == "pure"
