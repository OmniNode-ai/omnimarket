# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run facts a phase's tools act on, fixed by the node before the agent starts.

The agent never names a filesystem path, a branch or a command. Every path a tool touches
is resolved here from the run's date, the deployment overlay's values and the run host's
required environment, and every program a tool runs is a command the deployment overlay
names (its ``texts``), never one the agent supplies.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

#: artifact key -> the overlay value naming its knowledge-base path.
ARTIFACT_VALUES: dict[str, str] = {
    "ground_state": "GROUND_STATE_PATH",
    "delegation_accounting": "DELEGATION_ACCOUNTING_PATH",
    "date_snapshot": "SNAPSHOT_PATH",
    "rebaseline": "REBASELINE_PATH",
    "integration_plan": "INTEGRATION_PATH",
    "dropped_work": "DROPPED_WORK_PATH",
    "goal": "GOAL_PATH",
    "decisions_md": "DECISIONS_MD_PATH",
    "decisions_json": "DECISIONS_JSON_PATH",
}
#: The overlay texts naming the programs the tools run (each a command line).
COMMAND_TEXTS = (
    "ledger_reconcile_command",
    "decisions_register_command",
    "shared_doc_scrub_command",
    "kb_clone_refresh_command",
    "ledger_append_command",
    "delegation_step_command",
    "delegation_accounting_command",
    "closeout_completion_command",
    "closeout_scope_command",
    "triage_fences_command",
    "triage_started_command",
    "dod_verify_command",
    "kb_pr_create_command",
    "pr_handoff_command",
)
#: The overlay texts naming the run host's locations; ``$NAME`` reads the environment.
LOCATION_TEXTS = ("tools_root", "ledger_file", "kb_clone", "kb_worktree")
#: Optional locations: a tool that needs one the overlay does not name refuses.
PLACE_TEXTS = (
    "pr_watcher_state",
    "reports_dir",
    "process_readme",
    "venv_reconcile_status",
    "venv_reconcile_log",
    "clones_root",
    "state_root",
)
#: Optional overlay values the tools read as plain facts (not places, not programs).
FACT_VALUES = (
    "LINEAR_TEAM_ID",
    "PLANS_DIR",
    "PLAN_LIFECYCLE_PATH",
    "STANDING_CONTROL_PLAN",
)
#: The overlay text holding a regular expression the clone's origin URL must match.
KB_ORIGIN_TEXT = "kb_origin_pattern"
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class ModelMorningToolContext(BaseModel):
    """Everything a phase's tools may touch, resolved before the session starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase_label: str = Field(min_length=1)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    publish: bool
    root: Path
    ledger_path: Path
    kb_clone: Path
    kb_worktree: Path
    kb_branch: str
    kb_origin_pattern: str | None = None
    commit_prefix: str
    decisions_stale_days: int = Field(default=7, ge=1, le=60)
    #: artifact key -> knowledge-base relative path, the only files a tool reads or writes.
    artifacts: dict[str, str]
    #: command text name -> the argument vector it runs, every ``$NAME`` already expanded.
    commands: dict[str, list[str]]
    #: optional location text name -> its resolved path (PLACE_TEXTS).
    places: dict[str, Path] = Field(default_factory=dict)
    #: optional overlay facts (FACT_VALUES).
    facts: dict[str, str] = Field(default_factory=dict)
    #: The phase's own ledger lane, ticket and model, for its CLAIM and TERMINAL rows.
    lane: str | None = None
    ticket: str | None = None
    model: str = ""
    #: The run's private state directory (delegation drafts, the CLAIM stamp, read caches).
    state_dir: Path | None = None
    #: The request's window start (integrateSince) and closure-path probe pin.
    window_start: str = ""
    closure_probe_ticket: str = ""
    closure_probe_ceiling_s: int = Field(default=600, gt=0)
    #: source document key -> its workspace-relative path (the request's plan documents).
    source_docs: dict[str, str] = Field(default_factory=dict)
    integration_repos: list[str] = Field(default_factory=list)

    def artifact_path(self, key: str) -> str:
        if key not in self.artifacts:
            raise ValueError(
                f"unknown artifact key {key!r}; declared: {sorted(self.artifacts)}"
            )
        return self.artifacts[key]

    def command(self, name: str) -> list[str]:
        if name not in self.commands:
            raise ValueError(f"the deployment overlay names no {name!r}")
        return list(self.commands[name])

    def place(self, name: str) -> Path:
        if name not in self.places:
            raise ValueError(f"the deployment overlay names no {name!r}")
        return self.places[name]

    def state(self) -> Path:
        if self.state_dir is None:
            raise ValueError("this run has no state directory")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir


def expand(text: str) -> str:
    """``$NAME`` and ``${NAME}`` from the environment; an unset name refuses (no default)."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        found = os.environ.get(name, "").strip()
        if not found:
            raise RuntimeError(
                f"{name} is unset; the morning phase tools have no default"
            )
        return found

    return VARIABLE.sub(value, text)


def expand_command(text: str) -> list[str]:
    """One overlay command line as an argument vector, each word expanded."""
    return [expand(word) for word in shlex.split(text)]


def build_context(
    *,
    phase_label: str,
    day: str,
    publish: bool,
    values: dict[str, str],
    texts: dict[str, str],
) -> ModelMorningToolContext:
    """Resolve the context from the overlay and the run host's environment (no defaults)."""

    def dated(text: str) -> str:
        for name, value in values.items():
            text = text.replace(f"@@{name}@@", value)
        return text.replace("@@date@@", day)

    missing = [name for name in LOCATION_TEXTS if name not in texts]
    if missing:
        raise RuntimeError(f"the deployment overlay names no {missing}; no default")
    where = {name: Path(expand(dated(texts[name]))) for name in LOCATION_TEXTS}
    return ModelMorningToolContext(
        phase_label=phase_label,
        date=day,
        publish=publish,
        root=where["tools_root"],
        ledger_path=where["ledger_file"],
        kb_clone=where["kb_clone"],
        kb_worktree=where["kb_worktree"],
        kb_branch=dated(values["KB_BRANCH"]),
        kb_origin_pattern=texts.get(KB_ORIGIN_TEXT),
        commit_prefix=f"docs({values['WORKTREE_TICKET']}):",
        decisions_stale_days=int(values.get("DECISIONS_STALE_DAYS", "7")),
        artifacts={
            key: dated(values[name])
            for key, name in ARTIFACT_VALUES.items()
            if name in values
        },
        commands={
            name: expand_command(texts[name]) for name in COMMAND_TEXTS if name in texts
        },
        places={
            name: Path(expand(dated(texts[name])))
            for name in PLACE_TEXTS
            if name in texts
        },
        facts={name: values[name] for name in FACT_VALUES if name in values},
    )
