# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the registry module tick unit installer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRegistryUnitsRequest(BaseModel):
    """Install, render or inspect one registry_module_tick unit.

    ``project_path`` is the unit's project path override: a checkout of the unit's registry project (a branch
    worktree) that the rendered unit runs in place of the canonical clone ``registry_root/<registry_project>``.
    ``expected_revision`` is the commit that checkout must be at. ``path_overrides`` replaces the value of the
    environment variables the unit contract lists as path-overridable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: Literal["dry-run", "install", "status", "kickstart", "uninstall"] = "dry-run"
    home: str
    registry_root: str
    workspace_config_root: str = ""
    plans_path: str = ""
    marketplace_root: str = ""
    # A copied unit takes scripts and matching packages from this one plugin root.
    source_root: str = ""
    ledger_path: str = ""
    project_path: str = ""
    expected_revision: str = ""
    path_overrides: dict[str, str] = Field(default_factory=dict)
    # Persisted in units that declare this input; launchd does not inherit the
    # installing shell's environment. An empty value defers to that shell.
    runtime_ssh_host: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.@-]*)?$"
    )
    # The Phoenix base URL a unit that declares this input persists for its tick (the factory floor reads its trace
    # count and newest span age there). Persisted for the same reason as runtime_ssh_host; empty defers to the shell.
    phoenix_url: str = Field(default="", pattern=r"^(?:https?://[^\s/]+(?:/\S*)?)?$")
    # The checkout whose interpreter and ledger appender the alarm unit runs (its launchd PATH has none), and the host
    # table file the alarm reads each host's event names from. Persisted in the unit's install record, so a reinstall
    # with neither reproduces the configuration. Empty defers to the installing shell, then to the last install.
    appender_root: str = Field(default="", pattern=r"^(?:/[^\s]+)?$")
    host_table_path: str = Field(default="", pattern=r"^(?:/[^\s]+)?$")
    # The name the ledger knows the installing host by, persisted in a unit that declares this input (the factory floor
    # publisher's join J5 step and its ledger reads name it). Empty defers to the installing shell's ONEX_LEDGER_HOST_NAME.
    ledger_host_name: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]*)?$"
    )
    # The lab_hosts row this install is for; names the declared overlays clone. Empty defers to
    # ledger_host_name, then to the installing shell's ONEX_LEDGER_HOST_NAME.
    host_name: str = Field(default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]*)?$")
    # The remote-lane runner script the alarm starts for each episode it opens. Optional: a configuration that has no
    # value for it carries no dispatch_command, and the alarm sends no lane (it says so in its row).
    remote_lane_runner: str = Field(default="", pattern=r"^(?:/[^\s]+)?$")
    # The path of the operator's bootstrap env file the lab-state alarm reads two named keys from (the Slack bot token
    # and the alert channel id) when the unit's environment does not hold them. Only the path is persisted, never a value.
    alert_env_file: str = Field(default="", pattern=r"^(?:/[^\s]+)?$")
    # The ledger read command a unit that declares this input persists in place of the read command its contract renders
    # (OMN-23606): an absolute executable followed by plain words, no shell metacharacters. The factory floor's 14-day
    # window read took 23.5 s through the rendered command and 15.9 s through the internal ledger source on the launching
    # Mac (load 12 to 16), which put its 30 s step over the limit. Empty defers to the installing shell, then to the
    # contract's rendered command.
    ledger_read_command: str = Field(
        default="", pattern=r"^(?:/[^\s;&|`$<>()'\"\\]+(?: [^\s;&|`$<>()'\"\\]+)*)?$"
    )
    # The address of the Valkey the board's source read cache keeps its answers in (OMN-24050). Empty defers to the installing shell;
    # unset in both, the cache is off and each step reads its sources as it did. No password is a request input: a rendered unit is
    # printed by a dry run, so a secret never goes into it.
    source_cache_valkey_host: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]*)?$"
    )
    source_cache_valkey_port: str = Field(default="", pattern=r"^(?:[0-9]{1,5})?$")
    # The reference of the Valkey password the board's source read cache uses (OMN-24112): a store path
    # (secret://onex/<folder>/<KEY>) naming where the password is kept, never the password. A dry run prints the
    # rendered unit, so a value must never be a request input. Empty defers to the installing shell; unset in both,
    # the cache connects with no password.
    source_cache_valkey_password_ref: str = Field(
        default="",
        pattern=r"^(?:secret://onex/(?:[A-Za-z0-9][A-Za-z0-9_.-]*/)*[A-Za-z0-9][A-Za-z0-9_.-]*)?$",
    )


class ModelRegistryUnitsResult(BaseModel):
    """``status`` is ``success`` or ``failure``; ``rendered`` is the plist text a dry-run or install produced;
    ``project`` and ``revision`` are the project the unit runs and the commit it is at; ``project_is_canonical`` says
    whether that project is the canonical registry clone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: str
    status: str
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    rendered: str = ""
    project: str = ""
    revision: str = ""
    bundle_sha256: str = ""
    project_is_canonical: bool = True
    record: str = ""
    # The configuration document a unit that declares one is installed with, and where it is written.
    config: str = ""
    config_path: str = ""


class ModelCopiedUnitPayload(BaseModel):
    """Source-relative payload and runtime arguments from a unit's overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    copied_files: tuple[str, ...]
    copied_trees: tuple[str, ...]
    arguments: tuple[str, ...] = ()
