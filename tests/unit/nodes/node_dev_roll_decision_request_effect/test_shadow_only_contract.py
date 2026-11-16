# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shadow-only contract of node_dev_roll_decision_request_effect.

Parent OMN-23532, root R4: node_dev_roll_request_compute is subscribed to the
decision-request topic but nothing publishes it, and nothing here may publish
toward the deploy agent. The contract's topics are read from ``contract.yaml``
and the node's sources are scanned with the ast module: no deploy topic, no
``rebuild-requested``, no repository name outside docstrings, and in the
handlers no numeric constant beyond 0 and 1.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_dev_roll_decision_request_effect"
_CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))

PUBLISH_TOPICS = {
    "onex.cmd.omnimarket.dev-roll-decision-requested.v1",
    "onex.evt.omnimarket.dev-roll-decision-request-skipped.v1",
    "onex.evt.omnimarket.dev-roll-observation-recorded.v1",
}
SUBSCRIBE_TOPICS = {
    "onex.evt.omnimarket.dev-roll-manifest.v1",
    "onex.evt.omnimarket.dev-roll-decided.v1",
}
REPOSITORY_NAMES = ("omnimarket", "omnibase_infra", "origin/dev")

_DOCSTRING_KINDS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _python_files() -> list[Path]:
    return sorted(NODE_DIR.rglob("*.py"))


def _handler_files() -> list[Path]:
    return sorted((NODE_DIR / "handlers").glob("*.py"))


def _docstring_nodes(tree: ast.AST) -> set[ast.Constant]:
    """The string constants that are the docstrings of module, class or function."""
    found: set[ast.Constant] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_KINDS) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(first.value)
    return found


def _string_constants(path: Path) -> list[str]:
    """Every string constant of the file, outside docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    ]


def _int_constants(path: Path) -> list[int]:
    """Every integer constant of the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ]


def _source_findings(path: Path) -> list[str]:
    """The shadow-only violations one source file commits."""
    findings = []
    for value in _string_constants(path):
        if "onex.cmd.deploy" in value:
            findings.append(f"{path.name}: deploy topic literal {value!r}")
        if "rebuild-requested" in value:
            findings.append(f"{path.name}: rebuild-requested literal {value!r}")
        if value in REPOSITORY_NAMES:
            findings.append(f"{path.name}: repository name constant {value!r}")
    return findings


def test_the_contract_publishes_exactly_the_shadow_topics() -> None:
    assert set(_CONTRACT["event_bus"]["publish_topics"]) == PUBLISH_TOPICS


def test_the_published_events_name_the_same_shadow_topics() -> None:
    assert {entry["topic"] for entry in _CONTRACT["published_events"]} == PUBLISH_TOPICS


def test_no_published_topic_points_at_the_deploy_agent() -> None:
    for topic in PUBLISH_TOPICS:
        assert ".deploy." not in topic, f"{topic} is a deploy-agent topic"
        assert not topic.startswith("onex.cmd.deploy"), (
            f"{topic} is a deploy-agent command"
        )


def test_the_contract_subscribes_to_exactly_the_manifest_and_decided_topics() -> None:
    assert set(_CONTRACT["event_bus"]["subscribe_topics"]) == SUBSCRIBE_TOPICS


def test_no_source_holds_a_deploy_topic_or_rebuild_requested_literal() -> None:
    for path in _python_files():
        findings = [
            finding
            for finding in _source_findings(path)
            if "deploy topic" in finding or "rebuild-requested" in finding
        ]
        assert findings == [], f"{path} commits shadow-only violations: {findings}"


def test_no_source_names_a_repository_outside_docstrings() -> None:
    for path in _python_files():
        findings = [
            finding
            for finding in _source_findings(path)
            if "repository name" in finding
        ]
        assert findings == [], f"{path} names a repository in its code: {findings}"


def test_the_handlers_hold_no_numeric_constant_beyond_zero_and_one() -> None:
    for path in _handler_files():
        large = [value for value in _int_constants(path) if value not in (0, 1)]
        assert large == [], f"{path} carries numeric constants {large}"


_SYNTHETIC_SOURCE = "\n".join(
    [
        "TOPIC = 'onex.cmd.deploy.rebuild-requested.v1'",
        "REPOSITORY = 'omnimarket'",
    ]
)


def test_failing_control_the_scan_reports_a_synthetic_violation(tmp_path: Path) -> None:
    """The scan has teeth: the same scan is red on a source that names a deploy topic and a repository."""
    stand_in = tmp_path / "stand_in_node.py"
    stand_in.write_text(_SYNTHETIC_SOURCE, encoding="utf-8")
    findings = _source_findings(stand_in)
    assert findings != [], (
        "the scan found nothing in a source that violates the contract"
    )
    assert any("deploy topic" in finding for finding in findings)
    assert any("rebuild-requested" in finding for finding in findings)
    assert any("omnimarket" in finding for finding in findings)
