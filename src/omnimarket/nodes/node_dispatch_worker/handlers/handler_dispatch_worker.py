"""HandlerDispatchWorker — compile worker dispatch spec into role-templated agent prompt.

Pure prep node: reads TaskList state, validates inputs, compiles role templates with
collision fences, returns ModelDispatchWorkerResult. Makes NO external API calls,
no Agent() calls, no TaskCreate calls.

Template strings are STATIC — authored here, not user-configurable.
User-supplied values (name, scope, targets) are validated before entering
the template context dict. Role templates are versioned in contract.yaml
under template_version; CI invariant check blocks merge if content changes
without a version bump.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_result import (
    ModelDispatchWorkerResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role cap defaults (minutes)
# ---------------------------------------------------------------------------

ROLE_CAP_DEFAULTS: dict[EnumWorkerRole, int] = {
    EnumWorkerRole.watcher: 90,
    EnumWorkerRole.fixer: 90,
    EnumWorkerRole.designer: 120,
    EnumWorkerRole.auditor: 60,
    EnumWorkerRole.synthesizer: 90,
    EnumWorkerRole.sweep: 10,
    EnumWorkerRole.ops: 480,
}

ROLE_REPORT_CAPS: dict[EnumWorkerRole, int] = {
    EnumWorkerRole.watcher: 12,
    EnumWorkerRole.fixer: 20,
    EnumWorkerRole.designer: 15,
    EnumWorkerRole.auditor: 18,
    EnumWorkerRole.synthesizer: 15,
    EnumWorkerRole.sweep: 2,
    EnumWorkerRole.ops: 1,
}

# ---------------------------------------------------------------------------
# Common preamble (injected into all 7 role templates)
# ---------------------------------------------------------------------------

_COMMON_PREAMBLE = """\
You are `{name}` in team `{team}`.

## Identity
Claim the task whose subject starts with "[{role}] {name}:" via TaskUpdate (owner={name}, status=in_progress).

## Collision fences — ABSOLUTE RULE
You MUST NOT touch any of the following tickets, PRs, or files. They are owned by other workers:
{collision_fences_block}

## Worktree isolation
All code changes happen in {worktree_root}/{ticket}/{repo}/. NEVER commit inside the
canonical omni_home/<repo>/ clone. NEVER use --amend. NEW commits only.

## No sub-agents
You are a leaf worker. You MUST NOT spawn sub-agents or Agent() calls.

## Wall-clock cap
You have {wall_clock_cap_min} minutes. If not done, mark task in_progress with a blocker note
and SendMessage {reports_to} with your current state. Do not continue past the cap.

## Report format
When done (success or blocked), SendMessage to `{reports_to}` in ≤{report_line_cap} lines plain text.
Include: terminal state, artifacts produced (PR URL, file paths), blockers if any. Then stop.

## Stop rules
- On success: TaskUpdate status=completed, SendMessage {reports_to}, STOP.
- On blocker: TaskUpdate status=in_progress with note, SendMessage {reports_to}, STOP.
- Do NOT mark completed if tests are failing, implementation is partial, or PRs are not open.
"""

# ---------------------------------------------------------------------------
# Role-specific template bodies
# ---------------------------------------------------------------------------

_ROLE_WATCHER = """\
## What you do
Monitor {targets} for state transitions. You do NOT make code changes.

Loop:
1. gh pr checks {target_pr} --repo {target_repo} → classify as PASSING / FAILING / PENDING / MERGED
2. If MERGED → mark task completed, report, stop.
3. If FAILING → capture first failure reason (gh run view --log-failed). Include in report. Do not fix.
4. If PENDING → wait 5 min. Recheck. Cap at {wall_clock_cap_min} min total.
5. If still FAILING at cap → mark in_progress + blocker note, report failure reason, stop.

Report lines: state, PR URL, check status, first failure line if failing, wall-clock elapsed.
"""

_ROLE_FIXER = """\
## TDD-FIRST SEQUENCE — you cannot begin implementation without completing this

Step 1: Read the ticket contract.
  - Fetch the Linear ticket via mcp__linear-server__get_issue for each ticket in {targets}.
  - Find every acceptance criterion in the description and dod_evidence fields.
  - If mcp__linear-server__get_issue fails or returns empty dod_evidence: TaskUpdate in_progress
    with note 'TDD blocked: Linear contract unreadable', SendMessage {reports_to} with the error, STOP.
    Do not proceed to implementation.

Step 2: Write ONE failing integration test per acceptance criterion that implies a side effect.
  - Side effects that count: Kafka message emitted, Postgres row written, HTTP call made, file written.
  - Side effects that do NOT count: return value assertions, mock assertions, unit tests.
  - Write tests BEFORE any implementation code. The test must fail because the implementation
    does not yet exist — NOT because of an import error or missing fixture.
  - Test must use real observable effects: EventBusInmemory.capture(), SELECT COUNT(), Path.exists().
  - Marker: @pytest.mark.integration on every test in this sequence.

Step 3: Run the test. It MUST FAIL with the expected failure reason (not skip, not import error).
  Paste the EXACT failing output (last 20 lines) into TaskUpdate description field as evidence.
  Format: "TDD evidence: <paste>"

Step 4: Only after Step 3, begin implementation.

Step 5: Implementation is done when:
  (a) All integration tests in Step 2 pass.
  (b) All pre-existing tests still pass.
  (c) dod_verify confirms contract satisfied (if skill available).
  (d) hostile_reviewer has converged (see below).

## hostile_reviewer gate
After implementation, before PR creation, run:
  Skill(skill="onex:hostile_reviewer", args="<worktree_path>")
Address every MAJOR+ finding. Re-run. Converge (≤3 rounds). THEN open the PR.

## Additional fixer clauses
- Create worktree: {worktree_root}/{ticket}/{repo}/
- Push branch, open PR, enable auto-merge.
- Watch CI (same loop as watcher role). Cap total wall-clock at {wall_clock_cap_min} min.
"""

_ROLE_DESIGNER = """\
## What you produce
- {omni_home}/docs/design/{slug}-design.md (§1 through §N covering the problem space)
- {omni_home}/docs/plans/{date}-{slug}-plan.md (phased tasks, DoD per task, wave ordering)

## Design method
1. Read all relevant context listed in {targets}.
2. Draft design doc covering each §N in {scope}.
3. Run hostile_reviewer on your draft:
   Skill(skill="onex:hostile_reviewer", args="{omni_home}/docs/design/{slug}-design.md")
   Minimum 2 rounds. Address every MAJOR+ finding as a §Revision-N section.
4. Draft plan from converged design.
5. Plans dispatched for implementation must include TDD-first constraint in every task description.

## hostile_reviewer is MANDATORY
Do NOT do manual roleplay. Do NOT claim "the skill isn't available". The skill exists.
Skill(skill="onex:hostile_reviewer") is the ONLY acceptable invocation.

## No code changes
Design and plan only. Do NOT edit code files outside docs/design/ and docs/plans/.
No commits. No PRs. No Linear tickets (unless explicitly in {scope}).
"""

_ROLE_AUDITOR = """\
## What you do
Audit only. You do NOT modify files, commit, push, or file tickets.

1. Read all files/repos referenced in {targets}.
2. Produce findings doc at {omni_home}/docs/diagnosis-{slug}-{date}.md with:
   - §1 Methodology (what you read, what commands you ran)
   - §2 Findings table (item | severity | file path | recommendation)
   - §3 Summary counts and the single most alarming finding
   - §4 Recommended remediation tickets (recommend, do NOT file)

## Report format
Include: finding counts by severity, most alarming finding, doc path. ≤18 lines.
"""

_ROLE_SYNTHESIZER = """\
## What you do
Read the design docs listed in {targets}. Identify cross-domain interface conflicts and gaps.
Produce ONE consolidated reconciliation doc.

Output: {omni_home}/docs/design/{slug}-synthesized.md
Sections:
  - §Decisions (cross-domain decisions made)
  - §Conflicts (what was in conflict and how resolved)
  - §Open questions (unresolvable without human input)
  - §Interface contracts (explicit input/output contracts at every domain boundary)

Run hostile_reviewer on your synthesis before reporting:
  Skill(skill="onex:hostile_reviewer", args="{omni_home}/docs/design/{slug}-synthesized.md")
"""

_ROLE_SWEEP = """\
## What you do
Execute the sweep defined in {scope} across {targets}.
Report back ONE metrics line in exactly the format specified in {scope}.
No narrative. No summary. Just the metric line. Mark task completed. Stop.

Example: "merge_sweep :37 → merged=2 stuck=1 dirty=0 total_open=7"
"""

_ROLE_OPS = """\
## What you do
Wait for DMs from {reports_to} via inbox. Each DM is an admin request. Execute it. Reply one line.

Audit log: append every action to {omni_home}/.onex_state/{name}/actions-{date}.log
  Format: ISO timestamp | request summary | command | exit code | stdout first line

Supported actions: gh pr ready, gh pr merge, gh pr comment, gh pr edit, gh pr view,
  gh pr checks, gh run view, gh pr list, gh pr close, gh pr reopen, and any
  documented in {scope}.

Rules:
- NEVER modify code. gh commands only.
- NEVER run destructive actions without "I authorize destructive action" in the DM.
- One-line reply per action.
- Stay alive until {reports_to} sends shutdown_request.
- Do NOT mark task completed until shutdown_request received.

On startup:
  mkdir -p {omni_home}/.onex_state/{name}
  Write startup entry to audit log.
  SendMessage {reports_to} one line: "{name} ready, listening for requests"
"""

_ROLE_TEMPLATES: dict[EnumWorkerRole, str] = {
    EnumWorkerRole.watcher: _ROLE_WATCHER,
    EnumWorkerRole.fixer: _ROLE_FIXER,
    EnumWorkerRole.designer: _ROLE_DESIGNER,
    EnumWorkerRole.auditor: _ROLE_AUDITOR,
    EnumWorkerRole.synthesizer: _ROLE_SYNTHESIZER,
    EnumWorkerRole.sweep: _ROLE_SWEEP,
    EnumWorkerRole.ops: _ROLE_OPS,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TICKET_RE = re.compile(r"\bOMN-\d+\b")
_PR_RE = re.compile(r"([a-zA-Z0-9_-]+)#(\d+)")


def _slugify(text: str, max_len: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def _extract_primary_ticket(targets: list[str]) -> str:
    for t in targets:
        m = _TICKET_RE.search(t)
        if m:
            return m.group(0)
    return ""


def _extract_primary_repo(targets: list[str]) -> str:
    for t in targets:
        m = _PR_RE.search(t)
        if m:
            return m.group(1)
    return ""


def _extract_target_pr(targets: list[str]) -> str:
    for t in targets:
        m = _PR_RE.search(t)
        if m:
            return m.group(2)
    return ""


def _format_fence_block(fences: list[str]) -> str:
    if not fences:
        return "(none — no other in-progress workers detected at dispatch time)"
    return "\n".join(f"- {f}" for f in fences)


def _query_active_fences(
    team: str, current_targets: list[str], tasks_dir: Path | None = None
) -> list[str]:
    """Read TaskList snapshot and build collision fence set.

    Reads the task directory once atomically (point-in-time snapshot).
    Returns fence strings for all in-progress tasks whose targets overlap
    with other workers, excluding the current worker's own targets.

    collision_fences are a dispatch-time snapshot, not a live lock.
    """
    if tasks_dir is None:
        tasks_dir = Path(
            os.environ.get("CLAUDE_TASKS_DIR", "~/.claude/tasks")
        ).expanduser()

    team_dir = tasks_dir / team
    if not team_dir.exists():
        logger.warning("Task directory not found: %s — returning empty fence", team_dir)
        return []

    fences: list[str] = []
    current_target_set = {t.lower() for t in current_targets}

    try:
        for task_file in sorted(team_dir.iterdir()):
            if task_file.suffix != ".json":
                continue
            try:
                import json

                data = json.loads(task_file.read_text())
            except Exception:
                continue

            if not isinstance(data, dict):
                continue

            if data.get("status") != "in_progress":
                continue

            owner = data.get("owner", "unknown")
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                continue
            task_targets = metadata.get("targets", [])
            if isinstance(task_targets, str):
                task_targets = [task_targets]
            if not isinstance(task_targets, list):
                continue

            task_target_set = {t.lower() for t in task_targets if isinstance(t, str)}
            overlapping = task_target_set & current_target_set
            if not overlapping:
                continue

            # Emit the conflicting worker's OTHER targets (collision surface).
            # If all their targets overlap ours, fall back to the shared ones.
            non_own = task_target_set - current_target_set
            emit_set = non_own if non_own else overlapping
            for t in task_targets:
                if isinstance(t, str) and t.lower() in emit_set:
                    fences.append(f"{t} (owned by {owner})")
    except Exception as exc:
        logger.warning(
            "Failed to read task dir %s: %s — returning empty fence", team_dir, exc
        )
        return []

    return fences


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerDispatchWorker:
    """Compile worker dispatch spec into role-templated agent prompt.

    Pure prep — no external API calls, no Agent() calls, no TaskCreate.
    Template values are natural-language fields, not shell arguments.
    """

    def handle(
        self,
        command: ModelDispatchWorkerCommand,
        *,
        tasks_dir: Path | None = None,
        existing_task_subjects: list[str] | None = None,
    ) -> ModelDispatchWorkerResult:
        """Compile the dispatch spec.

        Args:
            command: Validated dispatch spec.
            tasks_dir: Override for task directory (used in tests).
            existing_task_subjects: Override list of existing task subjects (used in tests).
        """
        # --- deduplication check ---
        if existing_task_subjects is not None:
            subjects = existing_task_subjects
        else:
            subjects = self._read_task_subjects(command.team, tasks_dir=tasks_dir)

        rejection = self._check_dedup(command, subjects)
        if rejection:
            return ModelDispatchWorkerResult(
                validated_task_description="",
                validated_prompt_template="",
                proposed_agent_spawn_args={},
                collision_fence_embeds=[],
                rejected_reason=rejection,
            )

        # --- collision fences ---
        if command.collision_fences:
            fences = list(command.collision_fences)
        else:
            fences = _query_active_fences(
                command.team, command.targets, tasks_dir=tasks_dir
            )

        # --- compile template ---
        prompt = self._compile_prompt(command, fences)

        # --- task description ---
        task_desc = f"[{command.role}] {command.name}: {command.scope}"

        return ModelDispatchWorkerResult(
            validated_task_description=task_desc,
            validated_prompt_template=prompt,
            proposed_agent_spawn_args={
                "name": command.name,
                "team_name": command.team,
                "model": command.model,
                "subagent_type": "general-purpose",
            },
            collision_fence_embeds=fences,
            rejected_reason="",
        )

    def _check_dedup(
        self, command: ModelDispatchWorkerCommand, existing_subjects: list[str]
    ) -> str:
        """Return rejection reason if a live worker with same name exists."""
        # We check by looking at task subjects from caller-provided list.
        # The caller provides either the real TaskList or a test fixture.
        # "in_progress" state is encoded in the subjects list by convention;
        # callers pass only in_progress subjects when checking dedup.
        for subj in existing_subjects:
            if re.match(rf"^(?:\[[^\]]+\]\s+)?{re.escape(command.name)}(?::|$)", subj):
                if command.replace:
                    return ""  # replace requested, allow through
                return (
                    f"worker {command.name!r} is already in_progress. "
                    "Use replace=true to kill and restart."
                )
        return ""

    def _compile_prompt(
        self, command: ModelDispatchWorkerCommand, fences: list[str]
    ) -> str:
        cap = command.wall_clock_cap_min or ROLE_CAP_DEFAULTS[command.role]
        report_cap = ROLE_REPORT_CAPS[command.role]
        today = datetime.date.today().isoformat()
        slug = _slugify(command.scope)
        ticket = _extract_primary_ticket(command.targets)
        repo = _extract_primary_repo(command.targets)
        target_pr = _extract_target_pr(command.targets)
        target_repo = repo

        omni_home = os.environ.get("OMNI_HOME", os.path.expanduser("~/omni_home"))
        worktree_root = os.environ.get(
            "OMNI_WORKTREES", os.path.join(os.path.dirname(omni_home), "omni_worktrees")
        )

        ctx: dict[str, object] = defaultdict(
            str,
            {
                "name": command.name,
                "team": command.team,
                "role": command.role.value,
                "scope": command.scope,
                "targets": ", ".join(command.targets),
                "collision_fences_block": _format_fence_block(fences),
                "reports_to": command.reports_to,
                "wall_clock_cap_min": cap,
                "report_line_cap": report_cap,
                "ticket": ticket,
                "repo": repo,
                "target_pr": target_pr,
                "target_repo": target_repo,
                "slug": slug,
                "date": today,
                "omni_home": omni_home,
                "worktree_root": worktree_root,
            },
        )

        role_required_ids: dict[EnumWorkerRole, list[str]] = {
            EnumWorkerRole.watcher: ["target_pr", "repo"],
            EnumWorkerRole.fixer: ["ticket", "repo"],
            EnumWorkerRole.designer: ["slug"],
            EnumWorkerRole.auditor: ["slug"],
            EnumWorkerRole.synthesizer: ["slug"],
            EnumWorkerRole.sweep: [],
            EnumWorkerRole.ops: [],
        }
        missing = [k for k in role_required_ids.get(command.role, []) if not ctx[k]]
        if missing:
            raise ValueError(
                f"Role {command.role!r} requires identifiers {missing} "
                "but they could not be derived from targets. "
                "Include a repo#PR or OMN-XXXX ticket in targets."
            )

        role_body = _ROLE_TEMPLATES[command.role]
        preamble = _COMMON_PREAMBLE.format_map(ctx)
        body = role_body.format_map(ctx)
        return preamble + "\n" + body

    def _read_task_subjects(
        self, team: str, tasks_dir: Path | None = None
    ) -> list[str]:
        """Read in_progress task subjects from team task dir."""
        if tasks_dir is None:
            tasks_dir = Path(
                os.environ.get("CLAUDE_TASKS_DIR", "~/.claude/tasks")
            ).expanduser()

        team_dir = tasks_dir / team
        if not team_dir.exists():
            return []

        subjects: list[str] = []
        try:
            import json

            for task_file in sorted(team_dir.iterdir()):
                if task_file.suffix != ".json":
                    continue
                try:
                    data = json.loads(task_file.read_text())
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("status") == "in_progress":
                    subj = data.get("subject", "")
                    if subj and isinstance(subj, str):
                        subjects.append(subj)
        except Exception as exc:
            logger.warning("Failed to read task subjects from %s: %s", team_dir, exc)

        return subjects
