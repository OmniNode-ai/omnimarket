# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read registered default branches and exact heads from the PR watcher."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Self, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.handlers.claim_index import ticket_from_branch
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.rules_merge_sweep import ticket_of
from omnimarket.handlers.scm.handler_scm_local_state import (
    WATCHER_MAX_AGE,
    WATCHER_SCHEMA,
)
from omnimarket.models.overlay_documents.model_registry_documents import (
    ModelRegistryRepository,
    ModelRepositoryRegistryOverlay,
)
from omnimarket.nodes.node_red_main_alarm_effect.models import (
    Branch,
    EnumAlarmFailure,
    ModelAlarmFailure,
    ModelAlarmInventory,
    ModelAlarmTarget,
)

_TIP_MAX_AGE = timedelta(minutes=20)
_SETTINGS_MAX_AGE = timedelta(hours=2)


class _WatcherModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Repository(_WatcherModel):
    default_branch: str = ""


class _BranchTip(_WatcherModel):
    sha: str = ""
    tip_read_at: str = ""
    unread: dict[str, str] = {}


class _Protection(_WatcherModel):
    read_at: str = ""
    protection: str = ""
    rules_read: bool = False
    contexts: tuple[str, ...] = ()
    rule_contexts: tuple[str, ...] = ()

    def checks(self, now: datetime) -> tuple[str, ...]:
        if (
            self.protection not in ("protected", "unprotected")
            or not self.rules_read
            or not _fresh(self.read_at, now, _SETTINGS_MAX_AGE)
        ):
            raise ValueError("required check settings are unavailable or stale")
        return tuple(sorted(set(self.contexts) | set(self.rule_contexts)))


class _Settings(_WatcherModel):
    branches: dict[str, _Protection] = {}


class _PullFacts(_WatcherModel):
    base: str = ""
    head_sha: str = ""
    head_ref: str = ""
    title: str = ""


class _Pull(_WatcherModel):
    facts: _PullFacts = _PullFacts()


class _Merge(_PullFacts):
    merge_sha: str = ""


class _Snapshot(_WatcherModel):
    schema_version: int = Field(alias="schema")
    last_tick: AwareDatetime
    repos: dict[str, _Repository]
    branches: dict[str, _BranchTip]
    settings: dict[str, _Settings]
    prs: dict[str, _Pull] = {}
    merges: dict[str, _Merge] = {}

    @classmethod
    def load(cls, path: Path, now: datetime) -> Self:
        reading = cls.model_validate_json(path.read_bytes())
        if reading.schema_version != WATCHER_SCHEMA or not (
            timedelta() <= now - reading.last_tick <= WATCHER_MAX_AGE
        ):
            raise ValueError(
                "PR watcher state schema or last_tick is unreadable or stale"
            )
        return reading


def _fresh(stamp: str, now: datetime, max_age: timedelta) -> bool:
    if not stamp:
        return False
    value = datetime.fromisoformat(stamp)
    return value.tzinfo is not None and timedelta() <= now - value <= max_age


def _target(
    snapshot: _Snapshot,
    name: str,
    repo: str,
    declared: ModelRegistryRepository | None,
    now: datetime,
) -> ModelAlarmTarget | None:
    metadata = snapshot.repos.get(name)
    if not metadata or not metadata.default_branch:
        raise ValueError("default branch is unreadable")
    branch = metadata.default_branch
    if branch not in ("main", "dev"):
        return None
    settings = snapshot.settings.get(name)
    protection = settings.branches.get(branch) if settings else None
    if protection is None:
        raise ValueError(f"required check settings for {branch} are unreadable")
    required = protection.checks(now)
    if not required:
        return None
    return _target_for_branch(
        snapshot, name, repo, cast(Branch, branch), required, declared, now
    )


def _target_for_branch(
    snapshot: _Snapshot,
    name: str,
    repo: str,
    branch: Branch,
    required: tuple[str, ...],
    declared: ModelRegistryRepository | None,
    now: datetime,
) -> ModelAlarmTarget:
    tip = snapshot.branches.get(f"{name}@{branch}")
    if (
        not tip
        or tip.unread.get("tip")
        or not _fresh(tip.tip_read_at, now, _TIP_MAX_AGE)
    ):
        raise ValueError(f"default branch {branch} tip is unavailable or stale")

    pulls = {
        key: snapshot.prs[key].facts if key in snapshot.prs else merge
        for key, merge in snapshot.merges.items()
        if key.startswith(f"{name}#")
        and merge.base == branch
        and merge.merge_sha == tip.sha
    }
    pulls.update(
        (key, pull.facts)
        for key, pull in snapshot.prs.items()
        if key.startswith(f"{name}#")
        and pull.facts.base == branch
        and pull.facts.head_sha == tip.sha
    )
    tickets = {
        ticket.upper()
        for facts in pulls.values()
        if (ticket := ticket_of(facts.title) or ticket_from_branch(facts.head_ref))
    }
    return ModelAlarmTarget(
        repo=repo,
        branch=branch,
        head_sha=tip.sha,
        required_checks=required,
        owner=declared.owner if declared else None,
        related_prs=tuple(sorted(pulls)),
        related_tickets=tuple(sorted(tickets)),
    )


def read_inventory(
    path: Path,
    now: datetime,
    *,
    scm_owner: str,
    registry: ModelRepositoryRegistryOverlay | None = None,
) -> ModelAlarmInventory:
    snapshot = _Snapshot.load(path, now)
    registry = registry or load_structured_overlay(
        "repos_registry", ModelRepositoryRegistryOverlay
    )
    declared = {
        row.repo.split("/", 1)[1].casefold(): row for row in registry.repositories
    }
    # The watcher also declares monitored repos outside the clone list, such as
    # the private registry's own repos. The clone list contributes owner data.
    names = {
        row.repo.split("/", 1)[1].casefold(): row.repo.split("/", 1)[1]
        for row in registry.repositories
    }
    names.update((name.casefold(), name) for name in snapshot.repos)
    owners = {row.repo.split("/", 1)[0] for row in registry.repositories}
    owner = scm_owner or (next(iter(owners)) if len(owners) == 1 else "")
    if not owner:
        raise ValueError("cannot resolve the SCM organization of watcher repositories")
    targets: list[ModelAlarmTarget] = []
    failures: list[ModelAlarmFailure] = []
    for key, name in sorted(names.items()):
        entry = declared.get(key)
        repo = entry.repo if entry else f"{owner}/{name}"
        try:
            target = _target(snapshot, name, repo, entry, now)
        except ValueError as exc:
            failures.append(
                ModelAlarmFailure(
                    kind=EnumAlarmFailure.INVENTORY_UNREADABLE,
                    repo=repo,
                    detail=str(exc),
                )
            )
        else:
            if target is not None:
                targets.append(target)
    return ModelAlarmInventory(targets=tuple(targets), failures=tuple(failures))
