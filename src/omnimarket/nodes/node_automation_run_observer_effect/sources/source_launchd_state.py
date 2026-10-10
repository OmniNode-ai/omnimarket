# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Latest-only evidence: ``launchctl print <domain>/<label>``.

Only the newest run is readable: the ``runs`` counter and the ``last exit
code``. When the counter rose by ``n`` since the last poll, one run event is
reported for the newest run and ``unseen_runs`` is ``n - 1``: the runs this
observer did not see. The first poll sets the baseline and reports no unseen
runs. A counter that went down (the job was reinstalled) counts every run since
the reset as new. Start and finish times are not in this evidence; the run is
placed between the previous poll and this one.
"""

import re
import subprocess

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEvidenceSource,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.field_readers import (
    outcome_for_exit,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.host_command_runner import (
    ProtocolHostCommandRunner,
    SubprocessHostCommand,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.protocol_run_evidence_source import (
    ModelEvidenceRead,
    ModelEvidenceReadContext,
    ModelObservedRun,
)

_RUNS = re.compile(r"^\s*runs = (\d+)\s*$", re.MULTILINE)
_LAST_EXIT = re.compile(r"^\s*last exit code = (-?\d+)\s*$", re.MULTILINE)


class SourceLaunchdState:
    def __init__(self, runner: ProtocolHostCommandRunner | None = None) -> None:
        self._runner: ProtocolHostCommandRunner = runner or SubprocessHostCommand()

    @property
    def source(self) -> EnumAutomationEvidenceSource:
        return EnumAutomationEvidenceSource.LAUNCHD_STATE

    def read(self, context: ModelEvidenceReadContext) -> ModelEvidenceRead:
        entry = context.entry
        if context.launchd_domain is None or entry.evidence is None:
            return ModelEvidenceRead(
                cursor=context.cursor,
                unreadable="no launchd domain was given to read "
                f"{entry.trigger.native_id}",
            )
        target = f"{context.launchd_domain}/{entry.evidence.locator}"
        try:
            outcome = self._runner.run(["launchctl", "print", target])
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelEvidenceRead(
                cursor=context.cursor, unreadable=f"launchctl print {target}: {exc}"
            )
        if outcome.returncode != 0:
            return ModelEvidenceRead(
                cursor=context.cursor,
                unreadable=f"launchctl print {target} exited {outcome.returncode}: "
                f"{outcome.stderr.strip()}",
            )
        runs_found = _RUNS.search(outcome.stdout)
        if runs_found is None:
            return ModelEvidenceRead(
                cursor=context.cursor,
                unreadable=f"launchctl print {target} reports no run count",
            )
        total = int(runs_found.group(1))
        previous = context.cursor.runs_counter
        cursor = context.cursor.model_copy(
            update={"runs_counter": total, "last_checked_at": context.now}
        )
        if total == 0 or total == previous:
            return ModelEvidenceRead(cursor=cursor)
        exit_found = _LAST_EXIT.search(outcome.stdout)
        if exit_found is None:
            return ModelEvidenceRead(cursor=cursor)
        code = int(exit_found.group(1))
        gained = total if previous is None or total < previous else total - previous
        unseen = 0 if previous is None else gained - 1
        started = context.cursor.last_checked_at or context.now
        run = ModelObservedRun(
            run_id=f"{entry.evidence.locator}:runs={total}",
            started_at=started,
            finished_at=context.now,
            outcome=outcome_for_exit(code),
            exit_code=code,
            did_work_count=0,
            unseen_runs=max(unseen, 0),
            evidence_ref=f"launchctl print {target}",
        )
        return ModelEvidenceRead(runs=(run,), cursor=cursor)
