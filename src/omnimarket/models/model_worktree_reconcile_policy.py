# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a host lets the worktree reconcile node remove (the policy a host overlay sets)."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelWorktreeReconcilePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quiet_hours: float = Field(default=6.0, ge=0, allow_inf_nan=False)
    stale_hours: float = Field(default=72.0, ge=0, allow_inf_nan=False)
    # A worktree lock with no live lane, process, unit or ref lock and no
    # activity for this many hours is stale: the lane that left it is gone, so
    # the tree is unlocked and judged by the normal rules.
    stale_lock_hours: float = Field(default=24.0, ge=0, allow_inf_nan=False)
    # The exact lock-reason texts the lane runners write with `git worktree lock`.
    # Only a lock whose reason is listed here may ever be judged stale; a person's
    # lock (any other reason, or none) is kept and named. Empty: no lock is stale.
    lane_lock_reasons: tuple[str, ...] = ()
    # The ledger a stale-lock judgement rests on is read fresh each time: its newest
    # row must be younger than this many hours (an empty, rolled or frozen file is
    # not a ledger that shows a lane finished), the rolled files of the last
    # ledger_lookback_days days beside it are read too (0: the live file only), and
    # the read stops as unread when it would take more than ledger_max_bytes.
    ledger_max_age_hours: float = Field(default=6.0, gt=0, allow_inf_nan=False)
    ledger_lookback_days: int = Field(default=0, ge=0)
    ledger_max_bytes: int = Field(default=268435456, gt=0)
    allow_standalone_clone_removal: bool = True
    max_removals_per_run: int = Field(default=200, ge=0)
    # A standalone clone whose origin owner is not listed is always kept (a
    # driver source tree or a third-party tool is not a lane's scratch clone).
    # Empty means no standalone clone qualifies.
    allowed_remote_owners: tuple[str, ...] = ()
    # True: a tree is removed only when its work is merged (content_merged or
    # merged_pr_head). Every other removal rule, and every model verdict that would
    # remove, is held at KEEP with the rule's reason, so a host can be limited to
    # the merged class by its overlay.
    merged_only: bool = False
    # What a systemd EnvironmentFile the run user cannot open holds, declared by the host overlay: key = the file's absolute
    # path, value = the absolute paths it holds that matter to the live-path set (empty: it holds none). Nothing is read
    # with elevated rights and no file mode changes; an unreadable file the host does not declare still fails the snapshot.
    unreadable_environment_files: dict[str, tuple[str, ...]] = Field(
        default_factory=dict
    )
    # What a launchd definition (a plist) the run user cannot read or parse holds, declared by the host overlay: key = the
    # file's absolute path, value = the absolute paths it holds that matter to the live-path set (empty: it holds none).
    # Nothing is read with elevated rights and no file mode changes; an unreadable or malformed definition the host does
    # not declare still fails the snapshot.
    unreadable_launchd_definitions: dict[str, tuple[str, ...]] = Field(
        default_factory=dict
    )

    @field_validator("unreadable_environment_files")
    @classmethod
    def _declared_files_and_paths_are_absolute(
        cls, declared: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for file, paths in declared.items():
            if not file.startswith("/"):
                raise ValueError(
                    f"unreadable_environment_files key is not an absolute path: {file}"
                )
            for path in paths:
                if not path.startswith("/"):
                    raise ValueError(
                        f"unreadable_environment_files[{file}] holds a path"
                        f" that is not absolute: {path}"
                    )
        return declared

    @field_validator("unreadable_launchd_definitions")
    @classmethod
    def _declared_definitions_and_paths_are_absolute(
        cls, declared: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for file, paths in declared.items():
            if not file.startswith("/"):
                raise ValueError(
                    f"unreadable_launchd_definitions key is not an absolute path: {file}"
                )
            for path in paths:
                if not path.startswith("/"):
                    raise ValueError(
                        f"unreadable_launchd_definitions[{file}] holds a path"
                        f" that is not absolute: {path}"
                    )
        return declared
