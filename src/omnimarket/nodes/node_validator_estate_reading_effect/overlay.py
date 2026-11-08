# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_validator_estate_reading_effect: every setting of the read, none in code.

Found at ``<root>/node_validator_estate_reading_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_validator_estate_reading_effect"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelEstateRepo(_Frozen):
    """A repository read at a ref of its clone, and where its validator classes may live."""

    name: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    source_roots: tuple[str, ...] = Field(min_length=1)
    hook_config: str = Field(min_length=1)


class ModelGenericFormat(_Frozen):
    """What makes a validator class canonical and generic."""

    #: The base class a generic validator subclasses.
    base_class: str = Field(min_length=1)
    #: Where the base class lives, as a path inside the core repository.
    base_module_path: str = Field(min_length=1)
    #: The rule document sits at ``<module dir>/<contract_subdir>/<validator_id><contract_suffix>``.
    contract_subdir: str = Field(min_length=1)
    contract_suffix: str = Field(min_length=1)
    #: Keys that hold the rule list at the document's top level or under ``nested_key``.
    rules_key: str = Field(min_length=1)
    nested_key: str = Field(min_length=1)
    #: A module-level collection of at least this many string literals is a rule table in code.
    literal_table_min_strings: int = Field(ge=2)
    #: A module-level call to one of these names (``re.compile``) is a pattern written in code.
    literal_pattern_calls: tuple[str, ...] = ()
    #: Names that, found in a module, say it emits a validator trace on the bus.
    bus_trace_names: tuple[str, ...] = Field(min_length=1)
    #: A class whose name ends in one of these and does not subclass the base is a bespoke validator.
    bespoke_suffixes: tuple[str, ...] = Field(min_length=1)


class ModelHooksRule(_Frozen):
    #: Hooks counted: a ``repo:`` equal to one of these, or containing one of ``own_repo_fragments``.
    local_repo_value: str = Field(min_length=1)
    own_repo_fragments: tuple[str, ...] = ()
    pytest_marker: str = Field(min_length=1)


class ModelPlanSource(_Frozen):
    """The validators plan whose tasks are counted, and where a task's ticket counts as merged."""

    repo: str = Field(min_length=1)
    #: The variable naming the directory that holds the plan repository's clone.
    root_env: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    path: str = Field(min_length=1)
    #: Matches the heading line of a task; group 1 is the task id. A task's tickets are the ones its heading line names.
    task_pattern: str = Field(min_length=1)
    ticket_pattern: str = Field(min_length=1)
    #: Matches ``<task id> <ticket> [and|to <ticket>]`` entries (group 1 the task, group 2 the ticket list), read from
    #: the whole document, for tickets the plan minted after its headings were written.
    minted_pattern: str = Field(min_length=1)
    #: Tickets that are the plan's own or its parent's, never a task's.
    exclude_tickets: tuple[str, ...] = ()
    #: The repositories whose default branch is searched for a commit citing the ticket.
    merge_repos: tuple[str, ...] = Field(min_length=1)


class ModelEstatePublish(_Frozen):
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a row stays fresh on the floor; older, the page says so.
    publish_max_age_seconds: int = Field(ge=1)


class ModelEstateScheduleHost(_Frozen):
    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or absolute).
    venv: str = Field(min_length=1)


class ModelEstateOverlay(_Frozen):
    repos_root_env: str = Field(min_length=1)
    core_repo: str = Field(min_length=1)
    repos: tuple[ModelEstateRepo, ...] = Field(min_length=1)
    generic_format: ModelGenericFormat
    hooks: ModelHooksRule
    plan: ModelPlanSource
    ledger_append: ModelLedgerAppendRoute
    publish: ModelEstatePublish
    git_timeout_seconds: int = Field(ge=1)
    injected_fault: str = Field(min_length=1)
    #: Hosts that install the daily systemd user timer (``schedule install --host <name>``).
    schedule_hosts: tuple[ModelEstateScheduleHost, ...] = ()
    #: Environment the installed unit carries (the clones' root, the plan clone's root, the append command, the host).
    unit_environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> ModelEstateOverlay:
        names = [r.name for r in self.repos]
        if len(names) != len(set(names)):
            raise ValueError("a repository is declared twice")
        if self.core_repo not in names:
            raise ValueError("core_repo is not among the declared repositories")
        hosts = [h.name for h in self.schedule_hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a schedule host is declared twice")
        return self

    def overlay_hash(self) -> str:
        text = json.dumps(
            self.model_dump(mode="json", exclude={"ledger_append"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelEstateOverlay:
    return load_structured_overlay(NODE_NAME, ModelEstateOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelEstateOverlay",
    "ModelEstatePublish",
    "ModelEstateRepo",
    "ModelEstateScheduleHost",
    "ModelGenericFormat",
    "ModelHooksRule",
    "ModelPlanSource",
    "load_overlay",
]
