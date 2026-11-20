# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One release cut tick: read the release train's decision for omnimarket and act on it.

WHY. The release-staleness gate fails a repository whose oldest unreleased packaged commit is older
than 24 hours. omnimarket releases through the explicit ``release-cut.yml`` dispatch, so nothing
cuts a release unless something asks. A nightly tick meets the gate by cutting on time: it
dispatches the workflow when the train says CUT, and blocks by name when it does not. A BLOCK is
not retried by the timer, so a blocked night is an ad hoc cut the next morning.

WHY THE TRAIN'S DECISION AND NOT A SECOND ONE. ``scripts/ci/release_train.py`` in omnibase_infra
holds the lab-receipt, nearest-runtime-ancestor and green-CI premises, and its policy lists
omnimarket as ``report_only``. The tick materialises that script from ``origin/dev`` and runs its
``plan`` command against a copy of the policy with omnimarket's mode set to ``cut``, so the
readiness a cut needs is the readiness the train computes. The policy in the repository is never
edited.

SINGLE WRITER, NO LEASE. The one mutation is the workflow dispatch. ``release-cut.yml``
serialises per repository and refuses a version that is not ahead of the latest tag, and the tick
refuses to dispatch while a release-cut run is queued or in progress, so a second tick cannot
double-cut.

EVERY RUN IS RECORDED, the quiet ones included, in ``last-run.json`` under the state directory.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_release_cut_tick_effect.handlers.release_cut_decision import (
    ACTION_BLOCKED,
    ACTION_CUT_DISPATCHED,
    ACTION_NOTHING_TO_CUT,
    ESCALATE_AGE_HOURS,
    REPO,
    SCHEMA,
    block_key,
    cell,
    decide_outcome,
    read_decision,
    should_raise,
    stamp,
    status_row,
)
from omnimarket.nodes.node_release_cut_tick_effect.handlers.release_cut_io import (
    Appender,
    Notifier,
    Runner,
    active_release_runs,
    append_row,
    dispatch_cut,
    load_state,
    notify,
    oldest_unreleased_age_hours,
    previous_run_verdict,
    refresh_and_materialize,
    run_command,
    write_cut_policy,
    write_json,
)
from omnimarket.nodes.node_release_cut_tick_effect.models.model_release_cut_tick import (
    ModelReleaseCutOutcome,
    ModelReleaseCutTickRequest,
    ModelReleaseCutTickResult,
)

STATE_SUBDIR = ".onex_state/omnimarket-release-cut"
STATE_FILE = "state.json"
EVIDENCE_FILE = "last-run.json"


class HandlerReleaseCutTick:
    """Run one tick; the command runner, the ledger appender and the notifier can be injected."""

    def __init__(
        self,
        run: Runner | None = None,
        appender: Appender | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._run: Runner = run or run_command
        self._append: Appender = appender or append_row
        self._notify: Notifier = notifier or notify

    def evaluate(
        self, registry_root: Path, state: dict[str, Any], now: datetime
    ) -> ModelReleaseCutOutcome:
        """Everything up to the dispatch decision, with no mutation apart from a scratch directory."""
        run = self._run
        scratch = Path(tempfile.mkdtemp(prefix="omnimarket-release-cut-"))
        try:
            failure = refresh_and_materialize(registry_root, scratch, run)
            if failure is None:
                failure = write_cut_policy(
                    scratch / "config" / "release_train_policy.yaml",
                    scratch / "policy.yaml",
                )
            if failure is not None:
                return ModelReleaseCutOutcome(
                    action=ACTION_BLOCKED,
                    reason="train_inputs_unavailable",
                    detail=failure,
                )
            planned = run(
                [
                    sys.executable,
                    str(scratch / "scripts" / "ci" / "release_train.py"),
                    "plan",
                    "--policy",
                    str(scratch / "policy.yaml"),
                    "--clones-dir",
                    str(registry_root),
                    "--repos",
                    REPO,
                    "--runtime-path-validator",
                    str(scratch / "validator.py"),
                ]
            )
            decision = read_decision(planned.stdout)
            latest_tag = str((decision or {}).get("latest_tag", ""))
            age = oldest_unreleased_age_hours(registry_root, latest_tag, now, run)
            active, active_reason = active_release_runs(run)
            previous = previous_run_verdict(state, run)
            outcome = decide_outcome(
                decision=decision,
                plan_returncode=planned.returncode,
                plan_stderr=planned.stderr,
                age_hours=age,
                active_runs=active,
                active_reason=active_reason,
                previous=previous,
            )
            return outcome.model_copy(update={"previous_run": previous[0]})
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def handle(self, request: ModelReleaseCutTickRequest) -> ModelReleaseCutTickResult:
        registry_root = Path(request.registry_root)
        ledger = Path(request.ledger_path)
        state_dir = (
            Path(request.state_dir)
            if request.state_dir
            else registry_root / STATE_SUBDIR
        )
        dry_run = request.dry_run
        now = (
            datetime.fromisoformat(request.now.replace("Z", "+00:00")).astimezone(UTC)
            if request.now
            else datetime.now(UTC)
        )
        state_path = state_dir / STATE_FILE
        state = load_state(state_path)
        outcome = self.evaluate(registry_root, state, now)
        new_state = dict(state)
        if outcome.previous_run in ("ok", "failed") and not dry_run:
            # The verdict on the last dispatched run is read once; a failure is raised by this tick.
            new_state["dispatched_run_checked"] = True

        if outcome.action == ACTION_CUT_DISPATCHED and not dry_run:
            started, failure = dispatch_cut(self._run)
            if failure:
                outcome = outcome.model_copy(
                    update={
                        "action": ACTION_BLOCKED,
                        "reason": "release_cut_dispatch_failed",
                        "detail": failure,
                    }
                )
            else:
                run_id, _, url = started.partition(" ")
                outcome = outcome.model_copy(update={"run_url": url})
                new_state.update(dispatched_run_id=run_id, dispatched_run_checked=False)

        raised = should_raise(outcome, state)
        record: dict[str, Any] = {
            "schema": SCHEMA,
            "ran_at": stamp(now),
            "action": outcome.action,
            "reason": outcome.reason,
            "candidate_sha": outcome.candidate_sha,
            "candidate_version": outcome.candidate_version,
            "latest_tag": outcome.latest_tag,
            "unreleased_count": outcome.unreleased_count,
            "oldest_unreleased_age_hours": outcome.oldest_unreleased_age_hours,
            "run_url": outcome.run_url,
            "raised": raised,
            "dry_run": dry_run,
            "detail": outcome.detail,
        }

        escalated = False
        if outcome.action == ACTION_BLOCKED:
            age = outcome.oldest_unreleased_age_hours
            escalated = bool(age is not None and age >= ESCALATE_AGE_HOURS)
            if state.get("last_block_key") != block_key(outcome):
                new_state["escalated_at_age"] = False
            if escalated and raised:
                new_state["escalated_at_age"] = True
            new_state["last_block_key"] = block_key(outcome)
        else:
            new_state.pop("last_block_key", None)
            new_state.pop("escalated_at_age", None)

        row = ""
        error_message = ""
        notified: bool | None = None
        if not dry_run:
            if outcome.action == ACTION_CUT_DISPATCHED or raised:
                row = status_row(
                    outcome, now, escalated=escalated, ticket=request.ticket
                )
                command = (
                    [request.python, request.ledger_lock]
                    if request.ledger_lock
                    else list(request.ledger_command)
                )
                appended, detail = self._append(command, ledger, row)
                record["ledger_append"] = detail
                if not appended:
                    error_message = f"ledger append failed: {detail}"
                    # A refused row is a tick that did not raise; do not record the edge as seen.
                    new_state["last_block_key"] = state.get("last_block_key")
                    record["raised"] = False
            if raised:
                title = f"omnimarket release blocked: {outcome.reason}"
                message = (
                    f"{outcome.unreleased_count} unreleased commit(s) since "
                    f"{outcome.latest_tag}; {cell(outcome.detail, 160)}"
                )
                notified = self._notify(request.osascript, title, message)
                record["notified"] = notified
            write_json(state_path, new_state)
            write_json(state_dir / EVIDENCE_FILE, record)

        log_line = (
            f"omnimarket-release-cut: {outcome.action} reason={outcome.reason} "
            f"candidate={outcome.candidate_sha[:12] or '-'} "
            f"unreleased={outcome.unreleased_count}"
            + (f" run={outcome.run_url}" if outcome.run_url else "")
        )
        return ModelReleaseCutTickResult(
            status="partial" if outcome.action == ACTION_BLOCKED else "success",
            exit_code=0,
            outcome=outcome,
            raised=bool(record["raised"]),
            dry_run=dry_run,
            status_row=row,
            ledger_append=str(record.get("ledger_append", "")),
            notified=notified,
            log_line=log_line,
            error_message=error_message,
        )


__all__ = [
    "ACTION_BLOCKED",
    "ACTION_CUT_DISPATCHED",
    "ACTION_NOTHING_TO_CUT",
    "HandlerReleaseCutTick",
]
