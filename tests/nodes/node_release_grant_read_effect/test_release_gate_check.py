# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fresh boundary checks and private policy loading outside the head checkout."""

from __future__ import annotations

import builtins
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV, NodeOverlayError
from omnimarket.models.change_source.model_change_history import EnumAccountType
from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseGatePolicy,
)
from omnimarket.models.release_gate.model_release_grant_facts import (
    EnumGateBoundary,
    EnumGrantState,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseDecision,
    EnumReleaseKind,
    ModelReleaseActor,
    ModelReleasePullRequestFacts,
)
from omnimarket.nodes.node_release_grant_read_effect import (
    HandlerCheckReleaseGate,
    HandlerReadReleaseGrant,
    HandlerRecordAuthorizationReceipt,
)
from omnimarket.nodes.node_release_grant_read_effect.models.model_release_grant import (
    ModelCheckReleaseGateResult,
)
from omnimarket.nodes.node_release_grant_read_effect.overlay import (
    load_release_gate_policy,
)

from . import release_gate_fixtures as f

pytestmark = pytest.mark.unit


def _checker(
    source: f.HistorySource,
    ledger: f.RecordingLedger | None = None,
    loader: Callable[[Path | None], ModelReleaseGatePolicy] | None = None,
) -> HandlerCheckReleaseGate:
    return HandlerCheckReleaseGate(
        reader=HandlerReadReleaseGrant(
            source,
            ledger if ledger is not None else f.RecordingLedger(),
            f.FixedClock(),
        ),
        policy_loader=loader
        if loader is not None
        else lambda _head_checkout: f.policy(),
    )


def _overlay(root: Path, document: dict[str, Any]) -> Path:
    path = root / "release_gate/overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("granted", "exit_code", "decision"),
    [(True, 0, EnumReleaseDecision.ADMIT), (False, 1, EnumReleaseDecision.REFUSE)],
)
def test_required_check_admits_only_a_granted_release_pull_request(
    granted: bool,
    exit_code: int,
    decision: EnumReleaseDecision,
) -> None:
    events = f.standing_grant() if granted else (f.standing_grant()[0],)
    result = _checker(f.HistorySource(events)).handle(f.check_request())
    assert result.exit_code == exit_code
    assert result.decision.decision is decision
    assert result.decision.kind is EnumReleaseKind.RELEASE_PR


def test_unreadable_history_fails_closed_with_blocked_exit_code() -> None:
    result = _checker(f.HistorySource(raise_on_read=True)).handle(f.check_request())
    assert result.exit_code == 2
    assert result.decision.decision is EnumReleaseDecision.BLOCKED
    assert result.grant is not None
    assert result.grant.reason == "label_history_unreadable"


def test_unwired_checker_is_blocked() -> None:
    result = HandlerCheckReleaseGate().handle(f.check_request())
    assert result.exit_code == 2
    assert result.decision.decision is EnumReleaseDecision.BLOCKED
    assert result.decision.reason == "gate_not_wired"


def test_policy_loader_failure_is_blocked_without_exposing_exception_text() -> None:
    def broken_loader(head_checkout: Path | None) -> ModelReleaseGatePolicy:
        raise RuntimeError("fixture-private-exception-detail")

    source = f.HistorySource(f.standing_grant())
    result = _checker(source, loader=broken_loader).handle(f.check_request())
    assert result.exit_code == 2
    assert result.decision.reason == "policy_unreadable"
    assert "fixture-private-exception-detail" not in result.model_dump_json()
    assert source.reads == 0


def test_local_host_revocation_is_rechecked_at_check_merge_and_release() -> None:
    source, ledger = f.HistorySource(f.standing_grant()), f.RecordingLedger()
    checker = _checker(source, ledger)
    assert checker.handle(f.check_request()).exit_code == 0
    assert source.reads == 1
    source.histories[(f.REPOSITORY, 7)] += (
        f.unlabeled("remove-1", f.LATER, "fixture-operator"),
    )
    assert checker.handle(f.check_request()).exit_code == 1
    assert source.reads == 2
    merge = checker.handle(f.check_request(boundary=EnumGateBoundary.MERGE))
    assert merge.exit_code == 1
    assert source.reads == 3
    # The merge receipt operation also performs its own fresh read and refuses.
    receipt_ledger = f.RecordingLedger()
    receipt = HandlerRecordAuthorizationReceipt(
        source, receipt_ledger, f.FixedClock()
    ).handle(f.receipt_request())
    assert receipt.written is False
    assert receipt.reason == "no_grant_at_merge:label_removed"
    assert receipt.facts.boundary is EnumGateBoundary.MERGE
    assert [kind for kind, _ in receipt_ledger.rows] == ["release_gate_decision"]
    assert source.reads == 4
    release = checker.handle(f.check_request(boundary=EnumGateBoundary.RELEASE))
    assert release.exit_code == 1
    assert source.reads == 5
    assert [fields["boundary"] for _, fields in ledger.rows] == [
        "check",
        "check",
        "merge",
        "release",
    ]
    assert [fields["state"] for _, fields in ledger.rows] == [
        "granted",
        "not_granted",
        "not_granted",
        "not_granted",
    ]
    assert all(kind == "release_gate_decision" for kind, _ in ledger.rows)


def test_push_after_grant_refuses_the_new_head() -> None:
    pr = f.check_request().pull_request
    assert pr is not None
    result = _checker(f.HistorySource(f.pushed_after_grant())).handle(
        f.check_request(pull_request=pr.model_copy(update={"head_sha": f.NEW_HEAD}))
    )
    assert result.exit_code == 1
    assert result.grant is not None
    assert result.grant.reason == "label_before_head_push"


@pytest.mark.parametrize(
    ("run_identity", "exit_code"),
    [("fixture-train-run", 0), ("agent-run", 1), (None, 1)],
)
def test_train_authority_requires_the_identity_carried_by_the_run(
    run_identity: str | None,
    exit_code: int,
) -> None:
    request = f.check_request(
        actor=ModelReleaseActor(
            login="fixture-train",
            account_type=EnumAccountType.USER,
            run_identity=run_identity,
        ),
        pull_request=ModelReleasePullRequestFacts(
            number=7,
            title="release: v1.2.3",
            head_branch="fixture-release/x",
            head_sha=f.HEAD,
        ),
    )
    result = _checker(f.HistorySource((f.standing_grant()[0],))).handle(request)
    assert result.exit_code == exit_code
    assert result.grant is not None
    assert result.grant.state is EnumGrantState.NOT_GRANTED
    assert result.decision.kind is (
        EnumReleaseKind.TRAIN_RELEASE if exit_code == 0 else EnumReleaseKind.RELEASE_PR
    )


@pytest.mark.parametrize(
    "edited_file",
    [
        "overlays/release_gate/overlay.yaml",
        ".github/workflows/release-gate.yml",
        ".github/workflows/call-release-gate.yml",
    ],
)
def test_head_edits_cannot_supply_policy_or_change_the_running_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edited_file: str,
) -> None:
    head, private = tmp_path / "head", tmp_path / "private"
    head_root = head / "overlays"
    malicious = f.policy(
        role_logins={"operator": ("fixture-agent",), "cloud_owner": ()},
        agent_identities=(),
        train={"logins": ("fixture-agent",), "run_identities": ("agent-run",)},
    )
    _overlay(head_root, malicious.model_dump(mode="json"))
    if edited_file != "overlays/release_gate/overlay.yaml":
        edited = head / edited_file
        edited.parent.mkdir(parents=True, exist_ok=True)
        edited.write_text("fixture-head-controlled-workflow\n", encoding="utf-8")
    private_file = _overlay(private, f.policy().model_dump(mode="json"))
    private_policy = load_release_gate_policy({OVERLAY_ROOTS_ENV: str(private)})
    env = {OVERLAY_ROOTS_ENV: os.pathsep.join((str(head_root), str(private)))}
    checker = _checker(
        f.HistorySource((f.standing_grant()[0],)),
        loader=lambda head: load_release_gate_policy(env, head_checkout=head),
    )
    opened: list[Path] = []
    original_read_text, original_open = Path.read_text, builtins.open

    def recording_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(path.resolve())
        return original_read_text(path, *args, **kwargs)

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, os.PathLike)):
            opened.append(Path(file).resolve())
        return original_open(file, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "read_text", recording_read_text)
        patcher.setattr(builtins, "open", recording_open)
        result = checker.handle(
            f.check_request(
                head_checkout=str(head), changed_files=(edited_file, "pyproject.toml")
            )
        )
    assert result.exit_code == 1
    assert result.decision.decision is EnumReleaseDecision.REFUSE
    assert result.decision.kind is EnumReleaseKind.RELEASE_PR
    assert result.grant is not None
    assert result.grant.policy_digest == private_policy.digest()
    assert private_file.resolve() in opened
    assert all(not path.is_relative_to(head.resolve()) for path in opened)


def test_only_a_head_overlay_is_unreadable_policy_and_blocks_the_check(
    tmp_path: Path,
) -> None:
    head = tmp_path / "head"
    _overlay(head, f.policy().model_dump(mode="json"))
    env = {OVERLAY_ROOTS_ENV: str(head)}
    with pytest.raises(NodeOverlayError, match="release_gate"):
        load_release_gate_policy(env, head_checkout=head)
    source = f.HistorySource(f.standing_grant())
    result = _checker(
        source, loader=lambda head: load_release_gate_policy(env, head_checkout=head)
    ).handle(f.check_request(head_checkout=str(head)))
    assert result.exit_code == 2
    assert result.decision.reason == "policy_unreadable"
    assert source.reads == 0


def test_policy_loader_reads_a_valid_private_overlay(tmp_path: Path) -> None:
    _overlay(tmp_path, f.policy().model_dump(mode="json"))
    assert load_release_gate_policy({OVERLAY_ROOTS_ENV: str(tmp_path)}) == f.policy()


def test_missing_overlay_names_release_gate(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match="release_gate"):
        load_release_gate_policy({OVERLAY_ROOTS_ENV: str(tmp_path)})


@pytest.mark.parametrize(
    "problem", ["missing_key", "unknown_key", "missing_role_logins"]
)
def test_invalid_overlay_is_rejected_without_echoing_policy_values(
    tmp_path: Path, problem: str
) -> None:
    document = f.policy().model_dump(mode="json")
    if problem == "missing_key":
        del document["grant_label"]
    elif problem == "unknown_key":
        document["fixture_unknown_key"] = "fixture-private-value"
    else:
        del document["role_logins"]["operator"]
    _overlay(tmp_path, document)
    with pytest.raises(NodeOverlayError, match="release_gate") as error:
        load_release_gate_policy({OVERLAY_ROOTS_ENV: str(tmp_path)})
    assert "fixture-private-value" not in str(error.value)
    assert "fixture-release-grant" not in str(error.value)
    assert all(login not in str(error.value) for login in f.FIXTURE_LOGINS)


def test_policy_loader_rejects_bad_tag_patterns(tmp_path: Path) -> None:
    document = f.policy().model_dump(mode="json")
    document["tagger"]["tag_patterns"] = ["["]
    _overlay(tmp_path, document)
    with pytest.raises((NodeOverlayError, re.error)):
        load_release_gate_policy({OVERLAY_ROOTS_ENV: str(tmp_path)})


def test_checker_does_not_cache_authorization_between_calls() -> None:
    source = f.HistorySource((f.standing_grant()[0],))
    checker = _checker(source)
    assert checker.handle(f.check_request()).exit_code == 1
    source.histories[(f.REPOSITORY, 7)] = f.standing_grant()
    assert checker.handle(f.check_request()).exit_code == 0
    assert source.reads == 2


@pytest.mark.parametrize("boundary", list(EnumGateBoundary))
@pytest.mark.parametrize("story", ["granted", "not_granted", "blocked"])
def test_each_boundary_result_round_trips_through_json(
    boundary: EnumGateBoundary, story: str
) -> None:
    source = f.HistorySource(
        f.standing_grant() if story == "granted" else (f.standing_grant()[0],),
        raise_on_read=story == "blocked",
    )
    result = _checker(source).handle(f.check_request(boundary=boundary))
    assert result.exit_code == {"granted": 0, "not_granted": 1, "blocked": 2}[story]
    restored = ModelCheckReleaseGateResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.model_dump_json() == result.model_dump_json()


def test_a_pull_request_event_without_its_facts_is_blocked_not_a_crash() -> None:
    result = _checker(f.HistorySource(f.standing_grant())).handle(
        f.check_request(pull_request=None)
    )
    assert result.exit_code == 2
    assert result.decision.decision is EnumReleaseDecision.BLOCKED
    assert result.decision.reason == "pull_request_facts_missing"
