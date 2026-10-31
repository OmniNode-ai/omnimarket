# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release action decisions, configurable shapes and pure node bindings."""

from __future__ import annotations

import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.change_source.model_change_history import EnumAccountType
from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseActorShape,
    ModelReleaseGatePolicy,
    ModelReleaseShapes,
)
from omnimarket.models.release_gate.model_release_grant_facts import (
    EnumGateBoundary,
    EnumGrantState,
    ModelReleaseGrantFacts,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_release_action_classify_compute import (
    NodeReleaseActionClassifyCompute,
)
from omnimarket.nodes.node_release_action_classify_compute.handlers.handler_release_action_classify import (
    HandlerReleaseActionClassify,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseDecision as Decision,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseEvent as Event,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseKind as Kind,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    ModelReleaseActionDecision,
    ModelReleaseActionRequest,
    ModelReleaseActor,
    ModelReleasePullRequestFacts,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_NAME = "node_release_action_classify_compute"
NODE_DIR = ROOT / "src/omnimarket/nodes" / NODE_NAME
CONTRACT_PATH = NODE_DIR / "contract.yaml"


def _policy(**overrides: Any) -> ModelReleaseGatePolicy:
    facts: dict[str, Any] = {
        "grant_label": "fixture-release-grant",
        "grantor_roles": ("operator", "cloud_owner"),
        "role_logins": {
            "operator": ("fixture-operator",),
            "cloud_owner": ("fixture-cloud-owner",),
        },
        "agent_identities": ("fixture-agent",),
        "train": ModelReleaseActorShape(
            logins=("fixture-train",),
            run_identities=("fixture-train-run",),
            branch_prefixes=("fixture-release/",),
        ),
        "tagger": ModelReleaseActorShape(
            logins=("fixture-tagger",),
            run_identities=("fixture-tagger-run",),
            tag_patterns=(r"^dev-merge-\d+$",),
        ),
        "shapes": ModelReleaseShapes(
            pr_title_prefixes=("release:",),
            pr_labels=("release",),
            pr_branch_prefixes=("fixture-release/",),
            version_files=("pyproject.toml",),
            version_tag_patterns=(r"^v\d+\.\d+\.\d+",),
        ),
    }
    facts.update(overrides)
    return ModelReleaseGatePolicy(**facts)


def _actor(
    login: str,
    run_identity: str | None = None,
    account_type: EnumAccountType = EnumAccountType.USER,
) -> ModelReleaseActor:
    return ModelReleaseActor(
        login=login, run_identity=run_identity, account_type=account_type
    )


def _pr(
    title: str = "chore: tidy",
    branch: str = "fixture-feature/x",
    labels: tuple[str, ...] = (),
    number: int = 7,
    head_sha: str = "a" * 40,
) -> ModelReleasePullRequestFacts:
    return ModelReleasePullRequestFacts(
        title=title, head_branch=branch, labels=labels, number=number, head_sha=head_sha
    )


def _grant(state: EnumGrantState, **kw: Any) -> ModelReleaseGrantFacts:
    facts: dict[str, Any] = {
        "state": state,
        "reason": "fixture-grant-state",
        "repository": "fixture-repo",
        "pull_request": 7,
        "head_sha": "a" * 40,
        "boundary": EnumGateBoundary.CHECK,
        "policy_digest": "d",
        "read_at": datetime(2026, 10, 28, 12, tzinfo=UTC),
    }
    if state is EnumGrantState.GRANTED:
        facts.update(grant_event_id="1", applier_role="operator")
    facts.update(kw)
    return ModelReleaseGrantFacts(**facts)


def _request(**kw: Any) -> ModelReleaseActionRequest:
    facts: dict[str, Any] = {
        "repository": "fixture-repo",
        "event": Event.PULL_REQUEST,
        "actor": _actor("fixture-agent"),
        "policy": _policy(),
        "pull_request": _pr(title="release: v1.2.3"),
        "changed_files": (),
        "grant": _grant(EnumGrantState.NOT_GRANTED),
    }
    facts.update(kw)
    return ModelReleaseActionRequest(**facts)


def _classify(**kw: Any) -> ModelReleaseActionDecision:
    result = HandlerReleaseActionClassify().handle(_request(**kw))
    assert result.kind.value in result.line
    return result


def test_a_release_pull_request_from_an_agent_with_no_grant_is_not_admitted() -> None:
    result = _classify()
    assert (result.kind, result.decision) == (Kind.RELEASE_PR, Decision.REFUSE)


def test_the_trains_own_release_is_admitted_without_grant_facts() -> None:
    result = _classify(
        actor=_actor("fixture-train", "fixture-train-run"),
        pull_request=_pr(title="release: v1.2.3", branch="fixture-release/x"),
        grant=None,
    )
    assert (result.kind, result.decision) == (Kind.TRAIN_RELEASE, Decision.ADMIT)


def test_a_dev_merge_tag_by_the_tagger_is_admitted_without_grant_facts() -> None:
    result = _classify(
        event=Event.TAG_PUSH,
        ref="dev-merge-123",
        pull_request=None,
        actor=_actor("fixture-tagger", "fixture-tagger-run"),
        grant=None,
    )
    assert (result.kind, result.decision) == (Kind.DEV_MERGE_TAG, Decision.ADMIT)


@pytest.mark.parametrize(
    ("event", "ref", "kind"),
    [
        (Event.PULL_REQUEST, "", Kind.RELEASE_PR),
        (Event.TAG_PUSH, "v1.2.3", Kind.VERSION_TAG),
        (Event.PUBLISH_RUN, "", Kind.PACKAGE_PUBLISH),
        (Event.RELEASE_EVENT, "", Kind.GITHUB_RELEASE),
    ],
)
def test_each_release_kind_without_a_grant_is_refused_and_named(
    event: Event, ref: str, kind: Kind
) -> None:
    result = _classify(event=event, ref=ref)
    assert (result.kind, result.decision) == (kind, Decision.REFUSE)


@pytest.mark.parametrize(
    ("event", "ref", "kind"),
    [
        (Event.TAG_PUSH, "v1.2.3", Kind.VERSION_TAG),
        (Event.PUBLISH_RUN, "", Kind.PACKAGE_PUBLISH),
        (Event.RELEASE_EVENT, "", Kind.GITHUB_RELEASE),
    ],
)
@pytest.mark.parametrize(
    ("login", "role_login"),
    [
        ("fixture-collaborator", "fixture-collaborator"),
        ("FIXTURE-COLLABORATOR", "fixture-collaborator"),
        ("fixture-collaborator", "FIXTURE-COLLABORATOR"),
    ],
)
def test_a_collaborator_role_person_can_cut_their_own_release(
    event: Event, ref: str, kind: Kind, login: str, role_login: str
) -> None:
    policy = _policy(
        grantor_roles=("operator", "cloud_owner", "collaborator"),
        role_logins={**_policy().role_logins, "collaborator": (role_login,)},
    )
    result = _classify(
        event=event,
        ref=ref,
        actor=_actor(login),
        policy=policy,
        pull_request=None,
        grant=None,
    )
    assert (result.kind, result.decision, result.reason) == (
        kind,
        Decision.ADMIT,
        "role_holder_release",
    )


@pytest.mark.parametrize(
    ("event", "ref", "kind"),
    [
        (Event.TAG_PUSH, "v1.2.3", Kind.VERSION_TAG),
        (Event.PUBLISH_RUN, "", Kind.PACKAGE_PUBLISH),
        (Event.RELEASE_EVENT, "", Kind.GITHUB_RELEASE),
    ],
)
def test_only_roles_in_the_overlays_grantor_roles_authorise_releases(
    event: Event, ref: str, kind: Kind
) -> None:
    role_logins = {
        **_policy().role_logins,
        "collaborator": ("fixture-collaborator",),
    }
    facts: dict[str, Any] = {
        "event": event,
        "ref": ref,
        "actor": _actor("fixture-collaborator"),
        "pull_request": None,
        "grant": None,
    }
    refused = _classify(**facts, policy=_policy(role_logins=role_logins))
    assert (refused.kind, refused.decision) == (kind, Decision.REFUSE)
    admitted = _classify(
        **facts,
        policy=_policy(
            grantor_roles=("operator", "cloud_owner", "collaborator"),
            role_logins=role_logins,
        ),
    )
    assert (admitted.kind, admitted.decision, admitted.reason) == (
        kind,
        Decision.ADMIT,
        "role_holder_release",
    )


@pytest.mark.parametrize(
    ("event", "ref", "kind"),
    [
        (Event.TAG_PUSH, "v1.2.3", Kind.VERSION_TAG),
        (Event.PUBLISH_RUN, "", Kind.PACKAGE_PUBLISH),
        (Event.RELEASE_EVENT, "", Kind.GITHUB_RELEASE),
    ],
)
@pytest.mark.parametrize(
    ("actor", "agent_identities"),
    [
        (_actor("fixture-collaborator"), ("FIXTURE-COLLABORATOR",)),
        (
            _actor("fixture-collaborator", "FIXTURE-AGENT"),
            ("fixture-agent",),
        ),
        (
            _actor("fixture-collaborator", account_type=EnumAccountType.BOT),
            (),
        ),
    ],
)
def test_a_role_holder_that_is_an_agent_or_bot_cannot_cut_releases(
    event: Event,
    ref: str,
    kind: Kind,
    actor: ModelReleaseActor,
    agent_identities: tuple[str, ...],
) -> None:
    result = _classify(
        event=event,
        ref=ref,
        actor=actor,
        policy=_policy(
            grantor_roles=("operator", "cloud_owner", "collaborator"),
            role_logins={
                **_policy().role_logins,
                "collaborator": ("fixture-collaborator",),
            },
            agent_identities=agent_identities,
        ),
        pull_request=None,
        grant=None,
    )
    assert (result.kind, result.decision) == (kind, Decision.REFUSE)


def test_a_role_holders_release_pull_request_still_needs_a_grant() -> None:
    result = _classify(
        actor=_actor("fixture-collaborator"),
        policy=_policy(
            grantor_roles=("operator", "cloud_owner", "collaborator"),
            role_logins={
                **_policy().role_logins,
                "collaborator": ("fixture-collaborator",),
            },
        ),
        grant=_grant(EnumGrantState.NOT_GRANTED),
    )
    assert (result.kind, result.decision, result.reason) == (
        Kind.RELEASE_PR,
        Decision.REFUSE,
        "release_pr_without_grant",
    )


@pytest.mark.parametrize(
    ("event", "ref"),
    [
        (Event.TAG_PUSH, "v1.2.3"),
        (Event.PUBLISH_RUN, ""),
        (Event.RELEASE_EVENT, ""),
    ],
)
def test_the_trains_non_pull_request_releases_still_pass(
    event: Event, ref: str
) -> None:
    result = _classify(
        event=event,
        ref=ref,
        actor=_actor("fixture-train", "fixture-train-run"),
        policy=_policy(
            role_logins={**_policy().role_logins, "operator": ("fixture-train",)}
        ),
        pull_request=None,
        grant=None,
    )
    assert (result.kind, result.decision, result.reason) == (
        Kind.TRAIN_RELEASE,
        Decision.ADMIT,
        "train_release",
    )


@pytest.mark.parametrize(
    ("file", "verdict"),
    [("pyproject.toml", Decision.REFUSE), ("README", Decision.ADMIT)],
)
def test_unrecognised_actions_are_refused_only_when_touching_version_files(
    file: str, verdict: Decision
) -> None:
    result = _classify(event=Event.OTHER, pull_request=None, changed_files=(file,))
    assert (result.kind, result.decision) == (Kind.NONE, verdict)
    assert result.reason == (
        "unrecognised_action_touches_version_file"
        if file == "pyproject.toml"
        else "not_a_release"
    )


def test_a_plain_pull_request_editing_only_a_version_file_requires_a_grant() -> None:
    result = _classify(pull_request=_pr(), changed_files=("pyproject.toml",))
    assert (result.kind, result.decision) == (Kind.RELEASE_PR, Decision.REFUSE)


@pytest.mark.parametrize(
    ("grant", "reason"),
    [
        (None, "grant_facts_missing"),
        (_grant(EnumGrantState.BLOCKED), "grant_unreadable:fixture-grant-state"),
    ],
)
def test_missing_or_unreadable_grant_facts_block_a_release_pull_request(
    grant: ModelReleaseGrantFacts | None, reason: str
) -> None:
    result = _classify(grant=grant)
    assert (result.kind, result.decision) == (Kind.RELEASE_PR, Decision.BLOCKED)
    assert result.reason == reason


def test_a_plain_pull_request_with_unreadable_changed_files_is_blocked() -> None:
    result = _classify(pull_request=_pr(), changed_files=None)
    assert result.decision is Decision.BLOCKED
    assert result.reason == "changed_files_unreadable"


@pytest.mark.parametrize(
    "event", [Event.TAG_PUSH, Event.PUBLISH_RUN, Event.RELEASE_EVENT]
)
def test_no_grant_fact_ever_turns_a_refusal_into_an_admission(event: Event) -> None:
    for state in (EnumGrantState.NOT_GRANTED, EnumGrantState.GRANTED):
        result = _classify(event=event, ref="v1.2.3", grant=_grant(state))
        assert result.decision is Decision.REFUSE


def test_a_granted_release_pull_request_bound_to_this_change_is_admitted() -> None:
    result = _classify(grant=_grant(EnumGrantState.GRANTED))
    assert (result.kind, result.decision) == (Kind.RELEASE_PR, Decision.ADMIT)
    assert result.reason == "granted"


@pytest.mark.parametrize(
    "binding",
    [{"head_sha": "b" * 40}, {"pull_request": 8}, {"repository": "fixture-other-repo"}],
)
def test_a_grant_bound_to_another_change_is_refused(binding: dict[str, Any]) -> None:
    result = _classify(grant=_grant(EnumGrantState.GRANTED, **binding))
    assert result.decision is Decision.REFUSE
    assert result.reason == "grant_bound_to_another_change"


@pytest.mark.parametrize("run_identity", ["someone-else", None])
def test_a_train_login_and_branch_without_its_run_identity_are_not_the_train(
    run_identity: str | None,
) -> None:
    result = _classify(
        actor=_actor("fixture-train", run_identity),
        pull_request=_pr(title="release: v1.2.3", branch="fixture-release/x"),
    )
    assert (result.kind, result.decision) == (Kind.RELEASE_PR, Decision.REFUSE)


def test_an_actor_listed_as_both_train_and_agent_is_not_the_train() -> None:
    result = _classify(
        actor=_actor("fixture-train", "fixture-train-run"),
        pull_request=_pr(title="release: v1.2.3", branch="fixture-release/x"),
        policy=_policy(agent_identities=("fixture-agent", "fixture-train")),
    )
    assert (result.kind, result.decision) == (Kind.RELEASE_PR, Decision.REFUSE)


@pytest.mark.parametrize(
    ("login", "identity", "ref", "kind"),
    [
        ("fixture-tagger", "fixture-tagger-run", "v1.2.3", Kind.VERSION_TAG),
        ("fixture-other", "other-run", "dev-merge-123", Kind.NONE),
    ],
)
def test_tag_shape_and_actor_identity_must_both_be_authorised(
    login: str, identity: str, ref: str, kind: Kind
) -> None:
    result = _classify(event=Event.TAG_PUSH, ref=ref, actor=_actor(login, identity))
    assert (result.kind, result.decision) == (kind, Decision.REFUSE)


def test_changing_the_train_policy_changes_the_same_actions_verdict() -> None:
    facts: dict[str, Any] = {
        "actor": _actor("fixture-other", "other-run"),
        "pull_request": _pr(title="release: v9.9.9", branch="fixture-release/y"),
    }
    train = ModelReleaseActorShape(
        logins=("fixture-other",),
        run_identities=("other-run",),
        branch_prefixes=("fixture-release/",),
    )
    assert _classify(**facts).decision is Decision.REFUSE
    result = _classify(**facts, policy=_policy(train=train))
    assert (result.kind, result.decision) == (Kind.TRAIN_RELEASE, Decision.ADMIT)


@pytest.mark.parametrize(
    ("field", "value", "facts"),
    [
        ("version_files", ("VERSION",), {"changed_files": ("VERSION",)}),
        ("pr_labels", ("fixture-rc",), {"pull_request": _pr(labels=("fixture-rc",))}),
    ],
)
def test_configured_version_files_and_labels_move_pull_request_verdicts(
    field: str, value: tuple[str, ...], facts: dict[str, Any]
) -> None:
    action: dict[str, Any] = {"pull_request": _pr(), **facts}
    before = _classify(**action)
    assert (before.kind, before.decision) == (Kind.NONE, Decision.ADMIT)
    shapes = _policy().shapes.model_copy(update={field: value})
    after = _classify(**action, policy=_policy(shapes=shapes))
    assert (after.kind, after.decision) == (Kind.RELEASE_PR, Decision.REFUSE)


@pytest.mark.parametrize("version_tag", [True, False])
def test_configured_tag_patterns_change_the_same_tags_classification(
    version_tag: bool,
) -> None:
    policy = _policy()
    facts: dict[str, Any] = {
        "event": Event.TAG_PUSH,
        "ref": "fixture-tag-123",
        "actor": _actor("fixture-tagger", "fixture-tagger-run"),
    }
    before = _classify(**facts)
    assert (before.kind, before.decision) == (Kind.NONE, Decision.ADMIT)
    section = "shapes" if version_tag else "tagger"
    field = "version_tag_patterns" if version_tag else "tag_patterns"
    component = policy.shapes if version_tag else policy.tagger
    configured = _policy(
        **{section: component.model_copy(update={field: (r"^fixture-tag-\d+$",)})}
    )
    after = _classify(**facts, policy=configured)
    assert after.kind is (Kind.VERSION_TAG if version_tag else Kind.DEV_MERGE_TAG)
    assert after.decision is (Decision.REFUSE if version_tag else Decision.ADMIT)


def _protocol_dependency_paths(node: object, trail: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else str(key)
            if key == "protocol_dependencies":
                found.append(here)
            found.extend(_protocol_dependency_paths(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_protocol_dependency_paths(value, f"{trail}[{index}]"))
    return found


def test_the_contract_declares_no_protocol_dependencies() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert _protocol_dependency_paths(contract) == []
    # Positive control: the recursive scan finds a dependency nested in a list.
    contract["fixture"] = [{"protocol_dependencies": [{"name": "any"}]}]
    assert _protocol_dependency_paths(contract) == ["fixture[0].protocol_dependencies"]


@pytest.mark.parametrize(
    "grant", [None, _grant(EnumGrantState.NOT_GRANTED), _grant(EnumGrantState.GRANTED)]
)
def test_results_are_byte_identical_in_process_through_the_node_and_json(
    grant: ModelReleaseGrantFacts | None,
) -> None:
    request = _request(grant=grant)
    handler = HandlerReleaseActionClassify()
    result = handler.handle(request)
    assert result.kind.value in result.line
    encoded = result.model_dump_json().encode()
    assert handler.handle(request).model_dump_json().encode() == encoded
    node_result = NodeReleaseActionClassifyCompute().handle(request)
    assert node_result.model_dump_json().encode() == encoded
    restored = ModelReleaseActionRequest.model_validate_json(request.model_dump_json())
    assert handler.handle(restored).model_dump_json().encode() == encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login", "verdict"),
    [
        ("fixture-other", Decision.REFUSE),
        ("fixture-collaborator", Decision.ADMIT),
    ],
)
async def test_results_are_byte_identical_through_the_parity_harness_local_entry(
    login: str, verdict: Decision
) -> None:
    request = _request(
        event=Event.TAG_PUSH,
        ref="v1.2.3",
        actor=_actor(login),
        policy=_policy(
            grantor_roles=("operator", "cloud_owner", "collaborator"),
            role_logins={
                **_policy().role_logins,
                "collaborator": ("fixture-collaborator",),
            },
        ),
        pull_request=None,
        grant=None,
    )
    contract = yaml.safe_load(
        (NODE_DIR.parent / "node_binding_parity_orchestrator/contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    config = ModelRunLocalConfig.model_validate(contract["config"]["run_local"])
    decision = HandlerReleaseActionClassify().handle(request)
    returned = await run_local(
        NODE_NAME,
        "classify_release_action",
        request.model_dump(mode="json"),
        config,
    )
    assert json.dumps(decision.model_dump(mode="json"), sort_keys=True) == json.dumps(
        returned, sort_keys=True
    )
    assert decision.decision is verdict


def test_the_binding_table_checks_the_node_with_no_protocols() -> None:
    path = ROOT / "tests/nodes/node_binding_conformance_compute/data/binding_table.yaml"
    table = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert [row for row in table["rows"] if row["node"] == NODE_NAME] == [
        {"node": NODE_NAME, "protocols": []}
    ]
    assert NODE_NAME in table["checked_nodes"]


def test_the_node_names_no_host_path_repository_or_organization() -> None:
    forbidden = re.compile(
        r"192\.168\.|/(?:Users|Volumes)/|:/{2}|OmniNode-ai|omnimarket\.ai|\.git\b"
    )
    model_dir = ROOT / "src/omnimarket/models/release_gate"
    for directory in (NODE_DIR, model_dir):
        sources = [
            p
            for p in directory.rglob("*")
            if p.suffix in {".py", ".yaml"} and p.is_file()
        ]
        assert len(sources) >= (7 if directory == NODE_DIR else 3)
        assert [p.name for p in sources if forbidden.search(p.read_text("utf-8"))] == []


def test_the_node_is_a_registered_entry_point_with_matching_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entry[NODE_NAME] == f"omnimarket.nodes.{NODE_NAME}"
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text("utf-8"))
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == entry[NODE_NAME]
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert [
        route["operation"] for route in contract["handler_routing"]["handlers"]
    ] == ["classify_release_action"]


def test_the_contract_routes_one_command_to_one_terminal_event() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.release-action-classify-requested.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.release-action-classified.v1"
    ]
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.release-action-classified.v1"
    )
