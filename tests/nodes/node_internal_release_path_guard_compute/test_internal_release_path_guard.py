# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Internal consumers cannot be unblocked by publishing a package release."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_internal_release_path_guard import (
    ModelInternalReleasePathGuardRequest,
)
from omnimarket.nodes.node_internal_release_path_guard_compute import (
    NodeInternalReleasePathGuardCompute,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "text",
    [
        "Cut a release of omnibase_core to unblock omniclaude.",
        "Publish a new release, then bump the internal consumer.",
        "Wait for the next package release before moving the pin.",
        "Release omnibase-core so omniclaude can import the new model.",
        "Fix: ship a release of the sibling package.",
        "1. Cut a patch release.\n2. Update uv.lock.",
        "Run uv publish for the sibling before updating the consumer.",
        "The fix requires a new release of omnibase_core.",
        "Fix CI by cutting a release of core.",
        "Do not wait for a release, publish a release now.",
        "The tests did not pass; cut a release.",
        "The guard rejects unrelated failures but cut a release now.",
    ],
)
def test_refuses_release_remediation_for_internal_consumer(text: str) -> None:
    result = NodeInternalReleasePathGuardCompute().handle(
        ModelInternalReleasePathGuardRequest(proposed_actions=text)
    )
    assert not result.allowed
    assert result.exit_code == 1
    assert result.violations
    assert "tag" in result.remediation


@pytest.mark.parametrize(
    "text",
    [
        "Move the uv git source to dev-20261018T000000Z-0123456789ab after tests pass.",
        "Pin omnibase-core at commit 0123456789abcdef0123456789abcdef01234567.",
        "Do not cut a release for an internal consumer; use its dev tag.",
        "Never wait for a package release; consume a commit pin.",
        "Build a guard that refuses 'cut a release' as the internal remediation.",
        "No release is needed. Move to a tag.",
        "The previous release failed. Test the new tag.",
        "Read release notes before testing the git pin.",
    ],
)
def test_accepts_pins_and_prohibitions(text: str) -> None:
    result = NodeInternalReleasePathGuardCompute().handle(
        ModelInternalReleasePathGuardRequest(proposed_actions=text)
    )
    assert result.allowed
    assert result.exit_code == 0
    assert not result.violations


def test_structured_release_strategy_cannot_hide_behind_prose() -> None:
    result = NodeInternalReleasePathGuardCompute().handle(
        ModelInternalReleasePathGuardRequest(
            proposed_actions="Update the dependency.", dependency_strategy="release"
        )
    )
    assert not result.allowed


def test_external_release_is_in_scope_only_when_explicit() -> None:
    result = NodeInternalReleasePathGuardCompute().handle(
        ModelInternalReleasePathGuardRequest(
            proposed_actions="Publish a release for an outside customer.",
            consumer_scope="external",
            dependency_strategy="release",
        )
    )
    assert result.allowed


def test_tag_strategy_does_not_override_a_release_step() -> None:
    result = NodeInternalReleasePathGuardCompute().handle(
        ModelInternalReleasePathGuardRequest(
            proposed_actions="Cut a release first; then move to a dev tag.",
            dependency_strategy="git_tag",
        )
    )
    assert not result.allowed


def test_invalid_scope_is_rejected_instead_of_treated_as_external() -> None:
    with pytest.raises(ValidationError):
        ModelInternalReleasePathGuardRequest.model_validate(
            {"proposed_actions": "Cut a release.", "consumer_scope": "unknown"}
        )


def test_wire_roundtrip_and_contract_resolve_the_real_handler() -> None:
    node = ROOT / "src/omnimarket/nodes/node_internal_release_path_guard_compute"
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    handler_spec = contract["handler"]
    cls = getattr(
        importlib.import_module(handler_spec["module"]), handler_spec["class"]
    )
    request = ModelInternalReleasePathGuardRequest.model_validate_json(
        ModelInternalReleasePathGuardRequest(
            proposed_actions="Cut a release."
        ).model_dump_json()
    )
    result = cls().handle(request)
    assert not type(result).model_validate_json(result.model_dump_json()).allowed
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


def test_controller_worker_brief_checks_even_its_fixed_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from omnimarket.nodes.node_pr_landing_decision_compute.models import (
        model_landing_decision as m,
    )
    from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
        EnumLandingBriefClass,
        EnumLandingEngine,
    )

    cls = EnumLandingBriefClass.REAL_RED
    monkeypatch.setitem(
        m.BRIEF_INSTRUCTIONS, cls, "Cut a release of core to unblock the consumer."
    )
    with pytest.raises(ValidationError, match="internal-release-path"):
        m.ModelLandingWorkerBrief(
            brief_class=cls,
            pr="OmniNode-ai/omniclaude#1",
            head_sha="a" * 40,
            lease_id=1,
            engine=next(iter(EnumLandingEngine)),
            deadline_at=datetime.now(UTC),
            instructions=m.BRIEF_INSTRUCTIONS[cls],
        )


@pytest.mark.parametrize(
    ("brief", "allowed", "scope", "strategy"),
    [
        (
            "Cut a release to unblock the internal consumer.",
            False,
            "internal",
            "unspecified",
        ),
        (
            "Move to the sibling's dev tag after its integration tests pass.",
            True,
            "internal",
            "git_tag",
        ),
        ("Publish a release for an outside customer.", True, "external", "release"),
        ("Update the dependency.", False, "internal", "release"),
        ("Cut a release first; then use a tag.", False, "internal", "git_tag"),
    ],
)
def test_lab_fill_checks_before_writing_or_launching(
    tmp_path: Path, brief: str, allowed: bool, scope: str, strategy: str
) -> None:
    from omnimarket.models.lab_fill import ModelLabFillLaunch
    from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
        HandlerLabFillLaunch,
    )
    from omnimarket.nodes.node_lab_fill_effect.models import ModelLabFillLaunchRequest
    from tests.nodes.node_lab_fill_effect.fakes import (
        FakeApproved,
        FakeBlocks,
        FakeChecks,
        FakeClock,
        FakeRunner,
    )

    runner = FakeRunner()
    result = HandlerLabFillLaunch(
        checks=FakeChecks(),
        approved=FakeApproved(),
        blocks=FakeBlocks(),
        runner=runner,
        clock=FakeClock(),
    ).handle(
        ModelLabFillLaunchRequest(
            launch=ModelLabFillLaunch(
                lane="policy-test",
                ticket="OMN-19998",
                engine="codex",
                parent="parent",
                timeout_min=15,
            ),
            kind="process-fix",
            brief=brief,
            dependency_consumer_scope=scope,
            dependency_strategy=strategy,
            brief_dir=str(tmp_path / "briefs"),
            ledger_path=str(tmp_path / "ledger"),
        )
    )
    assert result.detached is allowed
    assert bool(runner.calls) is allowed
    assert (tmp_path / "briefs/policy-test.md").exists() is allowed
    if not allowed:
        assert result.skipped == "internal-release-path"


def test_cli_uses_node_refusal_exit_code() -> None:
    import json
    import subprocess
    import sys

    for text, code in [("Cut a release.", 1), ("Use a dev tag.", 0)]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_internal_release_path_guard_compute",
            ],
            input=json.dumps({"proposed_actions": text}),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == code, result.stderr
        assert json.loads(result.stdout)["allowed"] is (code == 0)


def test_controller_cause_brief_refuses_release_and_accepts_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from omnimarket.nodes.node_pr_landing_decision_compute.models import (
        model_landing_decision as m,
    )
    from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
        EnumLandingBriefClass,
        EnumLandingEngine,
    )

    cls = EnumLandingBriefClass.SHARED_CAUSE
    values = {
        "cause": "cause:OmniNode-ai/omniclaude:" + "b" * 12,
        "repo": "OmniNode-ai/omniclaude",
        "members": ({"pr": "OmniNode-ai/omniclaude#1", "head_sha": "a" * 40},),
        "attempt": 1,
        "lease_id": 1,
        "engine": next(iter(EnumLandingEngine)),
        "deadline_at": datetime.now(UTC),
    }
    original = m.BRIEF_INSTRUCTIONS[cls]
    assert (
        m.ModelLandingCauseBrief(**values, instructions=original).instructions
        == original
    )
    monkeypatch.setitem(
        m.BRIEF_INSTRUCTIONS,
        cls,
        "Wait for a package release before fixing the sibling pin.",
    )
    with pytest.raises(ValidationError, match="internal-release-path"):
        m.ModelLandingCauseBrief(**values, instructions=m.BRIEF_INSTRUCTIONS[cls])


def test_policy_is_required_in_ci_and_has_the_same_precommit_tests() -> None:
    import re

    from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["internal-release-path-guard"]
    assert job["name"] in STRICT_GATE_JOBS
    assert "if" not in job
    assert "needs" not in job
    hooks = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        h
        for repo in hooks["repos"]
        for h in repo["hooks"]
        if h["id"] == "internal-release-path-guard"
    )
    command = next(s["run"] for s in job["steps"] if "run" in s)
    assert hook["entry"].split() == command.split()
    assert hook["pass_filenames"] is False
    for filename in (
        "src/omnimarket/nodes/node_internal_release_path_guard_compute/contract.yaml",
        "src/omnimarket/nodes/node_internal_release_path_guard_compute/__init__.py",
        "src/omnimarket/nodes/node_lab_fill_effect/handlers/handler_lab_fill_launch.py",
        "src/omnimarket/nodes/node_pr_landing_decision_compute/models/model_landing_decision.py",
        "tests/nodes/node_internal_release_path_guard_compute/test_internal_release_path_guard.py",
        "tests/test_golden_chain_internal_release_path_guard_compute.py",
    ):
        assert re.search(hook["files"], filename), filename


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_action_is_not_a_passing_check(text: str) -> None:
    with pytest.raises(ValidationError):
        ModelInternalReleasePathGuardRequest(proposed_actions=text)
