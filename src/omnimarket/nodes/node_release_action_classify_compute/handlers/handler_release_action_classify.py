# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify release actions using only supplied facts and policy."""

from __future__ import annotations

import re

from omnimarket.models.change_source.model_change_history import EnumAccountType
from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseActorShape,
    ModelReleaseGatePolicy,
)
from omnimarket.models.release_gate.model_release_grant_facts import EnumGrantState
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseDecision,
    EnumReleaseEvent,
    EnumReleaseKind,
    ModelReleaseActionDecision,
    ModelReleaseActionRequest,
    ModelReleaseActor,
    ModelReleasePullRequestFacts,
)


def _is_agent(actor: ModelReleaseActor, policy: ModelReleaseGatePolicy) -> bool:
    identities = {identity.casefold() for identity in policy.agent_identities}
    return actor.login.casefold() in identities or (
        actor.run_identity is not None and actor.run_identity.casefold() in identities
    )


def _is_role_holder(actor: ModelReleaseActor, policy: ModelReleaseGatePolicy) -> bool:
    return (
        actor.account_type is EnumAccountType.USER
        and not _is_agent(actor, policy)
        and any(
            actor.login.casefold() == login.casefold()
            for role in policy.grantor_roles
            for login in policy.role_logins[role]
        )
    )


def _is_actor(
    actor: ModelReleaseActor,
    shape: ModelReleaseActorShape,
    policy: ModelReleaseGatePolicy,
) -> bool:
    return (
        actor.login.casefold() in {login.casefold() for login in shape.logins}
        and actor.run_identity is not None
        and actor.run_identity.casefold()
        in {identity.casefold() for identity in shape.run_identities}
        and not _is_agent(actor, policy)
    )


def _starts_with(value: str, prefixes: tuple[str, ...]) -> bool:
    return value.casefold().startswith(tuple(prefix.casefold() for prefix in prefixes))


def _touches_version_file(
    changed_files: tuple[str, ...], policy: ModelReleaseGatePolicy
) -> bool:
    return any(
        path == version_file or path.endswith("/" + version_file)
        for path in changed_files
        for version_file in policy.shapes.version_files
    )


def _release_shape(
    pr: ModelReleasePullRequestFacts,
    changed_files: tuple[str, ...] | None,
    policy: ModelReleaseGatePolicy,
) -> bool:
    shapes = policy.shapes
    return (
        _starts_with(pr.title, shapes.pr_title_prefixes)
        or bool(
            {label.casefold() for label in pr.labels}
            & {label.casefold() for label in shapes.pr_labels}
        )
        or _starts_with(pr.head_branch, shapes.pr_branch_prefixes)
        or (changed_files is not None and _touches_version_file(changed_files, policy))
    )


def _decision(
    kind: EnumReleaseKind, decision: EnumReleaseDecision, reason: str
) -> ModelReleaseActionDecision:
    return ModelReleaseActionDecision(
        kind=kind,
        decision=decision,
        reason=reason,
        line=f"{decision.value.upper()} {kind.value}: {reason}",
    )


def _classify_pull_request(
    request: ModelReleaseActionRequest,
) -> ModelReleaseActionDecision:
    pr = request.pull_request
    assert pr is not None
    policy = request.policy
    if not _release_shape(pr, request.changed_files, policy):
        if request.changed_files is None:
            return _decision(
                EnumReleaseKind.NONE,
                EnumReleaseDecision.BLOCKED,
                "changed_files_unreadable",
            )
        return _decision(
            EnumReleaseKind.NONE, EnumReleaseDecision.ADMIT, "not_a_release"
        )
    if _is_actor(request.actor, policy.train, policy) and (
        not policy.train.branch_prefixes
        or _starts_with(pr.head_branch, policy.train.branch_prefixes)
    ):
        return _decision(
            EnumReleaseKind.TRAIN_RELEASE, EnumReleaseDecision.ADMIT, "train_release"
        )
    kind = EnumReleaseKind.RELEASE_PR
    grant = request.grant
    if grant is None:
        return _decision(kind, EnumReleaseDecision.BLOCKED, "grant_facts_missing")
    if grant.state is EnumGrantState.BLOCKED:
        return _decision(
            kind, EnumReleaseDecision.BLOCKED, "grant_unreadable:" + grant.reason
        )
    if grant.state is EnumGrantState.NOT_GRANTED:
        return _decision(kind, EnumReleaseDecision.REFUSE, "release_pr_without_grant")
    if (grant.repository, grant.pull_request, grant.head_sha) != (
        request.repository,
        pr.number,
        pr.head_sha,
    ):
        return _decision(
            kind, EnumReleaseDecision.REFUSE, "grant_bound_to_another_change"
        )
    return _decision(kind, EnumReleaseDecision.ADMIT, "granted")


class HandlerReleaseActionClassify:
    """Pure release classification with grants scoped to pull requests."""

    def handle(self, request: ModelReleaseActionRequest) -> ModelReleaseActionDecision:
        policy = request.policy
        if request.event is EnumReleaseEvent.PULL_REQUEST:
            return _classify_pull_request(request)
        is_train = _is_actor(request.actor, policy.train, policy)
        if request.event is EnumReleaseEvent.TAG_PUSH:
            if any(
                re.match(pattern, request.ref)
                for pattern in policy.shapes.version_tag_patterns
            ):
                if is_train:
                    return _decision(
                        EnumReleaseKind.TRAIN_RELEASE,
                        EnumReleaseDecision.ADMIT,
                        "train_release",
                    )
                if _is_role_holder(request.actor, policy):
                    return _decision(
                        EnumReleaseKind.VERSION_TAG,
                        EnumReleaseDecision.ADMIT,
                        "role_holder_release",
                    )
                return _decision(
                    EnumReleaseKind.VERSION_TAG,
                    EnumReleaseDecision.REFUSE,
                    "version_tag_not_train",
                )
            if any(
                re.match(pattern, request.ref) for pattern in policy.tagger.tag_patterns
            ):
                if _is_actor(request.actor, policy.tagger, policy):
                    return _decision(
                        EnumReleaseKind.DEV_MERGE_TAG,
                        EnumReleaseDecision.ADMIT,
                        "dev_merge_tag",
                    )
                return _decision(
                    EnumReleaseKind.NONE,
                    EnumReleaseDecision.REFUSE,
                    "dev_merge_tag_not_tagger",
                )
        elif request.event in (
            EnumReleaseEvent.PUBLISH_RUN,
            EnumReleaseEvent.RELEASE_EVENT,
        ):
            if is_train:
                return _decision(
                    EnumReleaseKind.TRAIN_RELEASE,
                    EnumReleaseDecision.ADMIT,
                    "train_release",
                )
            if request.event is EnumReleaseEvent.PUBLISH_RUN:
                if _is_role_holder(request.actor, policy):
                    return _decision(
                        EnumReleaseKind.PACKAGE_PUBLISH,
                        EnumReleaseDecision.ADMIT,
                        "role_holder_release",
                    )
                return _decision(
                    EnumReleaseKind.PACKAGE_PUBLISH,
                    EnumReleaseDecision.REFUSE,
                    "package_publish_not_train",
                )
            if _is_role_holder(request.actor, policy):
                return _decision(
                    EnumReleaseKind.GITHUB_RELEASE,
                    EnumReleaseDecision.ADMIT,
                    "role_holder_release",
                )
            return _decision(
                EnumReleaseKind.GITHUB_RELEASE,
                EnumReleaseDecision.REFUSE,
                "github_release_not_train",
            )
        elif request.event is EnumReleaseEvent.OTHER:
            if request.changed_files is None:
                return _decision(
                    EnumReleaseKind.NONE,
                    EnumReleaseDecision.BLOCKED,
                    "changed_files_unreadable",
                )
            if _touches_version_file(request.changed_files, policy):
                return _decision(
                    EnumReleaseKind.NONE,
                    EnumReleaseDecision.REFUSE,
                    "unrecognised_action_touches_version_file",
                )
        return _decision(
            EnumReleaseKind.NONE, EnumReleaseDecision.ADMIT, "not_a_release"
        )
