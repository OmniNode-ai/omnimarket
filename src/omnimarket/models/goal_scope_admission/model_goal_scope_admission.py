# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The goal-scope admission models (OMN-23851), a port of the onex-overlays plugin's rule shapes.

The rule and the ticket scope arrive on the request, read from their overlays by the caller and
never from code; so do the ticket facts, gathered from the local Linear mirror or one live read,
and the tickets a read failed on. This module holds shapes only: it reads nothing, decides
nothing and hard-codes no deployment value. An unread ticket is never admitted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelGoalTicketRule(BaseModel):
    """The goal's ticket rule (OMN-22570), read from the ``goal_scope_ticket_rule`` overlay and never from code: an unlisted pull request
    of the operator is in scope when the ticket it declares is one of ``roots`` or a descendant of one (a child, a child's child, to
    ``max_depth`` steps), read from Linear. Every value is the overlay's: the endpoint the parent is read from, how long a read parent
    holds (``cache_seconds``), how many reads one tick may make, the bound of one read and the depth walked."""

    model_config = _FROZEN

    roots: frozenset[str]
    endpoint: str
    cache_seconds: int
    reads_per_tick: int
    read_timeout_seconds: int
    max_depth: int


class ModelTicketScope(BaseModel):
    """The shared ticket scope (OMN-23184), read from the ``ticket_scope`` overlay and never from code: an unlisted pull request of the
    operator is in scope when the ticket it declares, or any ancestor of it, sits in a Linear project the overlay lists, by id or by name. No
    stream mapping: the project is the ticket's own field. The endpoint, the bound of one read, how long a read project holds and how many reads
    one tick may make are the overlay's too."""

    model_config = _FROZEN

    project_ids: frozenset[str]
    project_names: frozenset[str]
    endpoint: str
    cache_seconds: int
    reads_per_tick: int
    read_timeout_seconds: int

    def lists(self, project_id: str, project_name: str) -> bool:
        return bool(project_id and project_id in self.project_ids) or bool(
            project_name and project_name in self.project_names
        )


class ModelTicketFacts(BaseModel):
    """What the one scope reader needs of a ticket (OMN-23184): its parent's identifier (None for a root of a tree) and its project, read
    from the local Linear mirror, else from one live read. A ticket in no project has an empty id and name. ``parent_known`` and
    ``project_known`` are False only for a fact bridged from the state files of the two readers this one replaced (one kept the parent, the
    other the project)."""

    model_config = _FROZEN

    parent: str | None = None
    project_id: str = ""
    project_name: str = ""
    parent_known: bool = True
    project_known: bool = True

    def covers(self, *, parent: bool, project: bool) -> bool:
        """Whether the fields asked for are known: a fact carried over from a reader that kept only one of them is partial, and what it
        does not know is read, never guessed."""
        return (self.parent_known or not parent) and (self.project_known or not project)


class ModelGoalCandidate(BaseModel):
    """What ``declared_ticket`` reads of one candidate change: its title and body, the two places a ticket is declared in."""

    model_config = _FROZEN

    title: str = ""
    body: str = ""


class ModelGoalScopeAdmissionRequest(BaseModel):
    """One admission pass: the candidates with the rule and the scope the caller read, the ancestry facts it already holds and the tickets a read failed on.

    Each candidate is keyed ``repo#n``. ``rule`` (OMN-22570) and ``scope`` (OMN-23184) arrive from their overlays as
    data, never from code; a request holding neither admits nothing. ``ticket_facts`` is keyed by ticket id;
    ``unread_tickets`` maps each ticket the caller tried to read and could not to why. A ticket whose asked-for
    facts were not supplied is unread, never a no."""

    model_config = _FROZEN

    candidates: dict[str, ModelGoalCandidate]
    rule: ModelGoalTicketRule | None = None
    scope: ModelTicketScope | None = None
    ticket_facts: dict[str, ModelTicketFacts] = Field(default_factory=dict)
    unread_tickets: dict[str, str] = Field(default_factory=dict)


class ModelGoalAdmission(BaseModel):
    """Why one candidate is in the goal: ``ticket`` when the ticket it declares, or an ancestor within the rule's ``max_depth`` hops, is one of
    the rule's ``roots`` (``label`` is the chain from the ticket up to the root, ``OMN-9<OMN-4<OMN-22478``); ``project`` when no root is on the
    chain and the ticket, or the first ancestor on the way up, sits in a project the scope lists (``label`` is that project for the ticket itself,
    else ``name@ancestor``)."""

    model_config = _FROZEN

    via: Literal["ticket", "project"]
    label: str


class ModelGoalScopeAdmissionResult(BaseModel):
    """One admission pass's answer: every candidate keyed ``repo#n`` under exactly one of the three.

    ``admitted`` carries the verdict; ``not_admitted`` the reason a candidate is out (it declares no ticket, no
    rule or scope is installed, or its ticket is under no goal root and in no listed project); ``unread`` the
    reason its ticket could not be read. An unread ticket is never admitted."""

    model_config = _FROZEN

    admitted: dict[str, ModelGoalAdmission] = Field(default_factory=dict)
    not_admitted: dict[str, str] = Field(default_factory=dict)
    unread: dict[str, str] = Field(default_factory=dict)
