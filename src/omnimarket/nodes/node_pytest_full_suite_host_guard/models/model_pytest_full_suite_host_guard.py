# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and decision for the full-suite pytest host guard (OMN-23367).

The pre-push guard script reads the committed host table, the private table and the per-login overlay
file, and hands the bytes to this node. The node merges the per-login rows (OMN-23355) and answers
whether a full suite may run on this host for this login. Nothing here reads a file or the environment.
"""

from pydantic import BaseModel, ConfigDict, Field


class ModelPytestFullSuiteHostGuardRequest(BaseModel):
    """The host tables as read, the host and login asking, and the row overrides in the environment.

    ``table_text`` is the committed table followed by the private table. ``login_overlay_text`` is the
    whole per-login overlay file, None when no overlay is on the roots. ``env_overrides`` holds the
    ``PREPUSH_*`` variables that replace a row's hostname. ``legacy_override_by_label`` comes from the
    node's overlay and is empty when no overlay is on the roots. ``login_home`` is the home directory of
    ``login`` as the host's account database gives it, empty when the account is unknown; relative
    uv and run-folder paths of a per-login row resolve under it. An empty ``host`` is an identity
    that could not be determined and is never refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_text: str
    login_overlay_text: str | None = None
    login: str
    login_home: str = ""
    host: str
    env_overrides: dict[str, str] = Field(default_factory=dict)
    legacy_override_by_label: dict[str, str] = Field(default_factory=dict)
    private_table_rel: str
    host_table_rel: str


class ModelPytestFullSuiteHostGuardDecision(BaseModel):
    """Whether a full suite may run here for this login, the row it resolves to, and why not.

    ``merged_table_text`` is the table with this login's uv and run folder in place of the host row's.
    ``row`` is the label of the authorizing row of the host, empty when the host is not designated.
    ``message`` is the refusal text when the host is not allowed and None otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    verdict: str
    row: str
    uv: str
    workroot: str
    designated_hostnames: tuple[str, ...]
    merged_table_text: str
    decision_line: str
    message: str | None
