# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect-node bypass scan (OMN-24432): golden and error chain event tests and the failing control.

Follows node_join_refusal_effect's test shape. The policy under test is the packaged contract's own ``config.scan``: nothing is
re-declared here. Golden: one site per category is counted and ``check`` against that baseline exits 0. Error: a baseline one
lower exits 1 and names the category; a missing baseline exits 3. Failing control: with the exclusions dropped, the excluded
paths count, so the exclusions are what keep them out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.events.effect_bypass import (
    EnumBypassCategory,
    ModelBypassInventory,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.handlers.handler_bypass_scan import (
    HandlerBypassScan,
    glob_to_regex,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.models import (
    ModelBypassScanCommand,
    ModelBypassScanPolicy,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.overlay import load_policy
from omnimarket.nodes.node_effect_bypass_scan_effect.runtime_bypass_scan import main

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_effect_bypass_scan_effect/contract.yaml"
)

# One call per category, each spelled the way a caller would write it.
LINES: dict[EnumBypassCategory, tuple[str, str]] = {
    EnumBypassCategory.GH_CLI: ("a.sh", "gh pr list --repo x/y"),
    EnumBypassCategory.GITHUB_API: (
        "b.py",
        'requests.get("https://api.github.com/repos/x")',
    ),
    EnumBypassCategory.GIT_REMOTE: ("c.sh", "git push origin HEAD"),
    EnumBypassCategory.SLACK_API: (
        "d.py",
        'URL = "https://hooks.slack.com/services/x"',
    ),
    EnumBypassCategory.LINEAR_API: ("e.py", 'URL = "https://api.linear.app/graphql"'),
    EnumBypassCategory.HOST_SSH: ("f.sh", "ssh labhost true"),
    EnumBypassCategory.DOCKER_ADMIN: ("g.sh", "docker compose up -d"),
    EnumBypassCategory.KAFKA_ADMIN: ("h.sh", "rpk topic list"),
}


class _Tree:
    """A lister over files written under a temp root."""

    def __init__(self, root: Path, files: dict[str, str]) -> None:
        self._files = tuple(files)
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def list_files(self, root: Path) -> tuple[str, ...]:
        return self._files


def _scan(
    root: Path, files: dict[str, str], policy: ModelBypassScanPolicy | None = None
) -> ModelBypassInventory:
    return HandlerBypassScan(policy or load_policy(), _Tree(root, files)).handle(
        ModelBypassScanCommand(repository="demo", root=str(root))
    )


def _golden_files() -> dict[str, str]:
    return {path: text + "\n" for path, text in LINES.values()}


def _baseline_file(tmp_path: Path, **counts: int) -> Path:
    body = {
        "schema_version": 1,
        "ticket": "OMN-24432",
        "repositories": {"demo": {"counts": counts}},
    }
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _every_category(**override: int) -> dict[str, int]:
    return {c.value: override.get(c.value, 1) for c in EnumBypassCategory}


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


def _check(root: Path, baseline: Path, files: dict[str, str]) -> int:
    return main(
        [
            "check",
            "--repository",
            "demo",
            "--root",
            str(root),
            "--baseline",
            str(baseline),
        ],
        file_lister=_Tree(root, files),
    )


def test_golden_one_site_per_category_and_check_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    inventory = _scan(root, _golden_files())
    assert {c: inventory.counts.get(c) for c in EnumBypassCategory} == dict.fromkeys(
        EnumBypassCategory, 1
    )
    assert {(s.path, s.line, s.category) for s in inventory.sites} == {
        (path, 1, category) for category, (path, _) in LINES.items()
    }
    baseline = _baseline_file(tmp_path, **_every_category())
    assert _check(root, baseline, _golden_files()) == 0
    assert "ok=True" in capsys.readouterr().out


def test_error_a_baseline_one_lower_fails_and_names_the_category(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    baseline = _baseline_file(tmp_path, **_every_category(host_ssh=0))
    assert _check(root, baseline, _golden_files()) == 1
    out = capsys.readouterr().out
    assert "host_ssh now=1 baseline=0 ROSE" in out
    assert "category_rose" in out


def test_error_a_missing_baseline_is_indeterminate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    assert _check(root, tmp_path / "none.yaml", {}) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


EXCLUDED = {
    "tests/x.py": "gh pr list\n",
    "docs/a.md": "gh pr list\n",
    "src/pkg/m.py": "# gh pr list\nx = 1\n",
    "src/pkg/n.py": '"""gh pr list"""\nx = 1\n',
    ".github/workflows/w.yml": "run: gh pr list\n",
    "contracts/OMN-1.yaml": "- gh pr list\n",
}


def test_excluded_paths_comments_and_docstrings_count_zero(tmp_path: Path) -> None:
    assert _scan(_root(tmp_path), EXCLUDED).counts.total == 0


def test_control_dropping_the_exclusions_counts_them(tmp_path: Path) -> None:
    """The injected break: no exclusions, and the excluded tree is no longer clean."""
    root = _root(tmp_path)
    policy = load_policy().model_copy(update={"exclude_globs": ()})
    broken = _scan(root, EXCLUDED, policy)
    assert broken.counts.get(EnumBypassCategory.GH_CLI) >= 4
    assert _scan(root, EXCLUDED).counts.total == 0


SANCTIONED_GH = "src/pkg/nodes/node_github_repo_write_effect/x.py"
OTHER_EFFECT_GH = "src/pkg/nodes/node_demo_effect/x.py"


def test_only_the_sanctioned_node_may_call_its_own_outside_world(
    tmp_path: Path,
) -> None:
    """The node that is GitHub's effect node is not a bypass of GitHub; another effect node that calls gh is."""
    root = _root(tmp_path)
    inventory = _scan(
        root, {SANCTIONED_GH: "gh pr list\n", OTHER_EFFECT_GH: "gh pr list\n"}
    )
    assert [(s.path, s.category) for s in inventory.sites] == [
        (OTHER_EFFECT_GH, EnumBypassCategory.GH_CLI)
    ]


def test_a_sanctioned_node_still_counts_for_every_other_outside_world(
    tmp_path: Path,
) -> None:
    inventory = _scan(_root(tmp_path), {SANCTIONED_GH: "ssh labhost true\n"})
    assert inventory.counts.get(EnumBypassCategory.HOST_SSH) == 1


def test_control_dropping_the_sanction_counts_the_sanctioned_node(
    tmp_path: Path,
) -> None:
    """The injected break: with no sanctioned nodes the GitHub effect node's own call is counted."""
    root = _root(tmp_path)
    policy = load_policy().model_copy(update={"sanctioned": {}})
    broken = _scan(root, {SANCTIONED_GH: "gh pr list\n"}, policy)
    assert broken.counts.get(EnumBypassCategory.GH_CLI) == 1
    assert _scan(root, {SANCTIONED_GH: "gh pr list\n"}).counts.total == 0


def test_a_line_in_two_categories_is_one_site_in_each(tmp_path: Path) -> None:
    inventory = _scan(_root(tmp_path), {"x.sh": "ssh labhost docker compose up\n"})
    assert inventory.counts.get(EnumBypassCategory.HOST_SSH) == 1
    assert inventory.counts.get(EnumBypassCategory.DOCKER_ADMIN) == 1


def test_glob_translation() -> None:
    assert glob_to_regex("**/tests/**").match("a/b/tests/c/d.py")
    assert glob_to_regex("**/tests/**").match("tests/c.py")
    assert not glob_to_regex("tests/**").match("src/tests_helper.py")
    assert glob_to_regex("**/test_*.py").match("a/test_x.py")
    assert not glob_to_regex("*.md").match("a/b.md")


def test_contract_declares_its_terminal_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.effect-bypass-scan-completed.v1"
    )
    assert contract["topics"]["effect_bypass_scan_failed"] == (
        "onex.evt.omnimarket.effect-bypass-scan-failed.v1"
    )


def test_the_policy_carries_every_category() -> None:
    assert set(load_policy().categories) == set(EnumBypassCategory)
