# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release and pin guard (OMN-18594): every refused shape, and the granted admit."""

from __future__ import annotations

from datetime import datetime

import pytest

from omnimarket.handlers.handler_release_pin_guard import REGISTRY, decide, parse_grants
from omnimarket.models.model_release_pin_guard import ModelReleasePinGuardRequest
from omnimarket.nodes.node_release_pin_guard_compute import NodeReleasePinGuardCompute

NOW = datetime(2026, 10, 23, 20, 0, 0)
CWD = "/work/omni_worktrees/OMN-1/omnibase_infra"


def _grant_row(
    items: str,
    *,
    approver: str = "operator",
    expires: str = "2026-10-24T07:00:00Z",
    stamp: str = "2026-10-23T19:30:00Z",
) -> str:
    return (
        f"{stamp} | OPERATOR-CONSENT | lane=orchestrator-31d9 | ticket=OMN-18594 | approved_by={approver} | "
        f'grant={items} | expires={expires} | "cut it" | APPROVED SCOPE: one release | OUT OF SCOPE: production\n'
    )


def _decide(tool: str, tool_input: dict, grants=(), current: str | None = None):
    return decide(
        tool,
        tool_input,
        cwd=CWD,
        grants=grants,
        registry=REGISTRY,
        current_file_text=current,
    )


REFUSED_BASH = [
    "gh release create v0.38.70 --repo OmniNode-ai/omnibase_infra",
    "gh workflow run release-train-nightly.yml --repo OmniNode-ai/omnibase_infra --ref dev -f repos=omnibase_infra",
    "gh workflow run release-cut.yml -R OmniNode-ai/omnimarket",
    "gh workflow run dependency-cascade-dispatch.yml -f package=omnibase_core",
    "gh workflow run omnimarket-contract-pin-refresh.yml",
    "gh api repos/OmniNode-ai/omnibase_infra/actions/workflows/release.yml/dispatches -f ref=dev",
    "gh api -X POST repos/OmniNode-ai/omnibase_infra/releases -f tag_name=v1.2.3",
    "gh api repos/OmniNode-ai/omnibase_infra/git/refs -f ref=refs/tags/v1.2.3 -f sha=abc",
    "git tag v0.38.70",
    "git -C /x/omnibase_infra tag -a v0.38.70 -m release",
    "git push origin v0.38.70",
    "git push origin refs/tags/v0.38.70",
    "git push --tags",
    "uv add omnibase-core==0.47.32",
    "uv lock --upgrade-package omnibase-core",
    "uv lock -P omnimarket",
    "uv lock --upgrade",
    "sed -i '' 's/omnibase-core==0.47.31/omnibase-core==0.47.32/' pyproject.toml",
    "cd /x && FOO=1 gh release create v1.0.0",
    'bash "${CLAUDE_PLUGIN_ROOT}/skills/release-cut/scripts/cut_facts.sh" omnibase_infra',
]


@pytest.mark.unit
@pytest.mark.parametrize("command", REFUSED_BASH)
def test_bash_shape_is_refused_without_a_grant(command: str) -> None:
    d = _decide("Bash", {"command": command})
    assert d.refuse, command
    assert "RULING 2026-10-23T17:13:55Z" in d.reason
    assert "/omni:operator-ruling" in d.reason
    assert "expires=" in d.reason


ADMITTED_BASH = [
    "git tag -l 'v*'",
    "git tag --list",
    "git push origin jonah/omn-18594-branch",
    "git push -u origin HEAD",
    "gh workflow run ci.yml",
    "gh pr create --title 'OMN-18594: x'",
    "uv lock",
    "uv sync --frozen",
    "uv run pytest tests/ -q",
    "grep -n omnibase-core==0.47 pyproject.toml",
    "sed -n 1,20p pyproject.toml",
    "git log --oneline v0.38.69..dev",
]


@pytest.mark.unit
@pytest.mark.parametrize("command", ADMITTED_BASH)
def test_ordinary_bash_is_admitted(command: str) -> None:
    assert not _decide("Bash", {"command": command}).refuse, command


@pytest.mark.unit
@pytest.mark.parametrize("skill", ["omni:release-cut", "onex:release"])
def test_release_skill_is_refused(skill: str) -> None:
    d = _decide("Skill", {"skill": skill, "args": "omnimarket"})
    assert d.refuse
    assert d.repo == "omnimarket"


@pytest.mark.unit
def test_other_skills_are_admitted() -> None:
    assert not _decide(
        "Skill", {"skill": "omni:ledger-write", "args": "CLAIM --lane x"}
    ).refuse


PIN_EDITS = [
    (
        "/w/omnibase_infra/pyproject.toml",
        'override-dependencies = ["omnibase-core==0.47.31"]',
        'override-dependencies = ["omnibase-core==0.47.32"]',
    ),
    (
        "/w/omnibase_infra/pyproject.toml",
        "",
        'omnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", rev = "'
        + "a" * 40
        + '" }',
    ),
    (
        "/w/omnibase_infra/.github/sibling-pins.yaml",
        "  omnimarket: " + "c" * 40,
        "  omnimarket: " + "d" * 40,
    ),
    (
        "/w/omnibase_infra/.github/omnimarket-contract-pin.yaml",
        "omnimarket_contract_ref: " + "c" * 40,
        "omnimarket_contract_ref: " + "e" * 40,
    ),
    (
        "/w/omnibase_infra/.github/workflows/ci.yml",
        "          ref: " + "1" * 40,
        "          ref: " + "2" * 40,
    ),
    (
        "/w/omnibase_infra/.github/workflows/ci.yml",
        "    uses: OmniNode-ai/omniclaude/.github/workflows/x.yml@" + "3" * 40,
        "    uses: OmniNode-ai/omniclaude/.github/workflows/x.yml@" + "4" * 40,
    ),
    (
        "/w/omnimarket/pyproject.toml",
        'omnibase-infra = { git = "https://github.com/OmniNode-ai/omnibase_infra", tag = "dev-20261023T1800Z-abcdef123456" }',
        'omnibase-infra = { git = "https://github.com/OmniNode-ai/omnibase_infra", tag = "dev-20261023T1900Z-123456abcdef" }',
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(("path", "old", "new"), PIN_EDITS)
def test_pin_edit_is_refused(path: str, old: str, new: str) -> None:
    d = _decide("Edit", {"file_path": path, "old_string": old, "new_string": new})
    assert d.refuse, (path, new)


@pytest.mark.unit
def test_write_compares_against_the_file_it_replaces() -> None:
    old = 'dependencies = ["omnibase-core>=0.47.31,<0.48"]\n'
    assert not _decide(
        "Write",
        {"file_path": "/w/x/pyproject.toml", "content": old + "# note\n"},
        current=old,
    ).refuse
    assert _decide(
        "Write",
        {
            "file_path": "/w/x/pyproject.toml",
            "content": 'dependencies = ["omnibase-core>=0.47.40,<0.48"]\n',
        },
        current=old,
    ).refuse


@pytest.mark.unit
def test_multiedit_pin_bump_is_refused() -> None:
    d = _decide(
        "MultiEdit",
        {
            "file_path": "/w/x/pyproject.toml",
            "edits": [
                {"old_string": "# a", "new_string": "# b"},
                {"old_string": "omnimemory==0.9.1", "new_string": "omnimemory==0.9.2"},
            ],
        },
    )
    assert d.refuse
    assert d.repo == "omnimemory"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "/w/x/pyproject.toml",
            'requires = ["hatchling>=1.25"]',
            'requires = ["hatchling>=1.26"]',
        ),
        ("/w/x/tests/test_pins.py", "", 'PIN = "omnibase-core==0.47.32"'),
        ("/w/x/docs/notes.md", "", "omnibase-core==0.47.32"),
        ("/w/x/src/mod.py", "", 'x = "omnibase-core==0.47.32"'),
        (
            "/w/x/pyproject.toml",
            'dependencies = ["omnibase-core>=0.47,<0.48"]',
            'dependencies = [\n  "omnibase-core>=0.47,<0.48",\n  "httpx>=0.27",\n]',
        ),
    ],
)
def test_non_pin_edits_are_admitted(path: str, old: str, new: str) -> None:
    assert not _decide(
        "Edit", {"file_path": path, "old_string": old, "new_string": new}
    ).refuse


@pytest.mark.unit
def test_a_grant_admits_exactly_its_repo_and_version() -> None:
    grants = parse_grants(
        [_grant_row("release:omnibase_infra@0.38.70,pin:omnibase_core@0.47.32")], NOW
    )
    assert not _decide(
        "Bash",
        {"command": "gh release create v0.38.70 --repo OmniNode-ai/omnibase_infra"},
        grants,
    ).refuse
    assert _decide(
        "Bash",
        {"command": "gh release create v0.38.71 --repo OmniNode-ai/omnibase_infra"},
        grants,
    ).refuse
    assert _decide(
        "Bash",
        {"command": "gh release create v0.4.400 --repo OmniNode-ai/omnimarket"},
        grants,
    ).refuse
    assert not _decide(
        "Skill", {"skill": "omni:release-cut", "args": "omnibase_infra"}, grants
    ).refuse
    assert not _decide(
        "Edit",
        {
            "file_path": "/w/omnibase_infra/pyproject.toml",
            "old_string": "omnibase-core==0.47.31",
            "new_string": "omnibase-core==0.47.32",
        },
        grants,
    ).refuse
    assert _decide(
        "Edit",
        {
            "file_path": "/w/omnibase_infra/pyproject.toml",
            "old_string": "omnibase-core==0.47.31",
            "new_string": "omnibase-core==0.47.33",
        },
        grants,
    ).refuse


@pytest.mark.unit
@pytest.mark.parametrize(
    "row",
    [
        _grant_row(
            "release:omnibase_infra@0.38.70", expires="2026-10-23T19:59:00Z"
        ),  # expired
        _grant_row(
            "release:omnibase_infra@0.38.70", approver="orchestrator"
        ),  # not an approver
        _grant_row("release:omnibase_infra@*"),  # names no version
        _grant_row(
            "release:omnibase_infra@0.38.70", stamp="2026-10-23T21:00:00Z"
        ),  # written in the future
        _grant_row("release:omnibase_infra@0.38.70").replace(
            "OPERATOR-CONSENT", "MSG"
        ),  # not a consent row
        _grant_row("release:omnibase_infra@0.38.70").replace(
            " | expires=2026-10-24T07:00:00Z", ""
        ),  # no expiry
    ],
)
def test_rows_that_grant_nothing(row: str) -> None:
    assert parse_grants([row], NOW) == ()


@pytest.mark.unit
def test_cloud_owner_grant_counts() -> None:
    assert parse_grants(
        [_grant_row("release:omnimarket@0.4.400", approver="cloud-owner")], NOW
    )


@pytest.mark.unit
def test_node_refuses_then_admits_with_a_grant() -> None:
    command = "git tag v0.38.70 && git push origin v0.38.70"
    call = {
        "tool_name": "Bash",
        "cwd": CWD,
        "tool_input": {"command": command},
        "now": NOW,
    }
    refused = NodeReleasePinGuardCompute().handle(ModelReleasePinGuardRequest(**call))
    assert refused.refuse
    assert refused.exit_code == 1
    assert "RULING 2026-10-23T17:13:55Z" in refused.reason
    granted = NodeReleasePinGuardCompute().handle(
        ModelReleasePinGuardRequest(
            **call, consent_rows=(_grant_row("release:omnibase_infra@0.38.70"),)
        )
    )
    assert not granted.refuse
    assert granted.exit_code == 0
    assert granted.grant == "2026-10-23T19:30:00Z"
