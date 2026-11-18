# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the closer reads and writes: node_linear_ticket_write_effect (tickets, comments, state), dod_verify (checks), a declared ledger reader (readings).

Each source is behind :class:`ProtocolSprintCloserSources` so the workflow test can run with a fake.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol, cast

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.scm.handler_scm_local_state import HandlerScmLocalState
from omnimarket.models.model_ticket_io import ModelTicketProjectDates
from omnimarket.nodes.node_sprint_closer_effect.handlers.handler_sprint_closer import (
    FINGERPRINT_PREFIX,
    HandlerSprintCloser,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_prs import (
    HandlerSprintPullRequests,
    current_project,
    pull_request_links,
)
from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumChangeKind,
    EnumCheckStatus,
    EnumCloserAction,
    ModelCheckResult,
    ModelSprintCloserPlan,
    ModelTicketEvidence,
    ModelTicketOutcome,
)

_CRITERION = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*\**\s*(AC\d+)\b", re.MULTILINE)
_RUNNING_READING = re.compile(r"(?:observed=)?SEEN[ _]RUNNING(?:$|:| \()")
_FP = re.compile(re.escape(FINGERPRINT_PREFIX) + r"([0-9a-f]{16})")


def parse_criteria(description: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """AC labels on checkbox lines, and those ticked. A label repeated in the text counts once."""
    labels: list[str] = []
    ticked: list[str] = []
    for mark, label in _CRITERION.findall(description or ""):
        if label not in labels:
            labels.append(label)
        if mark.lower() == "x" and label not in ticked:
            ticked.append(label)
    return tuple(labels), tuple(ticked)


def last_fingerprint(comment_bodies: list[str]) -> str | None:
    """The newest closer fingerprint among the comments (newest first)."""
    for body in comment_bodies:
        m = _FP.search(body or "")
        if m:
            return m.group(1)
    return None


def map_dod_checks(
    state: dict[str, object],
) -> tuple[str | None, str | None, tuple[ModelCheckResult, ...]]:
    """dod_verify's state JSON -> (contract ref, contract error, checks). verified is a pass, failed a fail."""
    raw = state.get("checks")
    checks: list[ModelCheckResult] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", ""))
        mapped = {"verified": EnumCheckStatus.PASS, "failed": EnumCheckStatus.FAIL}.get(
            status, EnumCheckStatus.NOT_RUN
        )
        detail = " ".join(str(item.get("message") or "").split())[:300]
        binds = item.get("binds_ac")
        checks.append(
            ModelCheckResult(
                evidence_id=str(item.get("evidence_id", "")),
                status=mapped,
                binds=tuple(str(b) for b in binds) if isinstance(binds, list) else (),
                detail="" if mapped is EnumCheckStatus.PASS else detail,
            )
        )
    if (
        len(checks) == 1
        and checks[0].evidence_id == "contract"
        and checks[0].detail.startswith("CONTRACT_MISSING:")
    ):
        return None, checks[0].detail, ()
    repo = state.get("contract_repository")
    sha = str(state.get("contract_commit_sha") or "")[:12]
    path = state.get("contract_repo_path")
    ref = f"{repo}@{sha}:{path}" if repo else None
    return ref, None, tuple(checks)


def read_seen_running(
    ticket_id: str, ledger_read_argv: tuple[str, ...]
) -> tuple[str, ...]:
    """Ledger rows that name the ticket in a ticket= cell and read SEEN RUNNING (or SEEN_RUNNING).

    The rows come from the ledger reader the host declares in ``ONEX_SPRINT_CLOSER_LEDGER_READ_ARGV`` (a JSON argv that
    prints rows on stdout); with none declared the closer sees no readings and leaves tickets open.
    """
    if not ledger_read_argv:
        return ()
    proc = subprocess.run(
        list(ledger_read_argv),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ledger reader exited {proc.returncode}: {proc.stderr.strip()[-300:]}"
        )
    hits: list[str] = []
    cell = f"ticket={ticket_id}"
    for line in proc.stdout.splitlines():
        cells = [part.strip() for part in line.split("|")]
        if cell in cells and any(
            part in {"verdict=SEEN_RUNNING", "verdict=SEEN RUNNING"}
            or _RUNNING_READING.match(part)
            for part in cells[1:]
        ):
            hits.append(f"ledger row {cells[0]}")
    return tuple(hits)


class ProtocolSprintCloserSources(Protocol):
    def list_ticket_ids(self, plan: ModelSprintCloserPlan) -> list[str]: ...

    def run_ticket(
        self, plan: ModelSprintCloserPlan, ticket_id: str
    ) -> ModelTicketOutcome: ...


NODE_ID = "node_linear_ticket_write_effect"
CLOSED_STATES = frozenset({"Done", "Canceled", "Cancelled", "Duplicate"})
STATE_FILE = "closer-comments.json"


class ProtocolNodeRunner(Protocol):
    def __call__(self, payload: dict[str, object]) -> dict[str, object]: ...


@dataclass(frozen=True)
class LinearIssue:
    """One ticket as the write node's read answers it, plus the closer's own last comment body."""

    identifier: str
    title: str
    description: str
    state: str
    assignee: str | None
    comment_bodies: tuple[str, ...] = ()
    attachment_urls: tuple[str, ...] = ()

    @property
    def closed(self) -> bool:
        return self.state in CLOSED_STATES


@dataclass
class HandlerOnexNodeCall:
    """Runs node_linear_ticket_write_effect as the node's declared CLI (``onex node``) and returns its terminal payload.

    The input is one JSON request file, the answer is the ``terminal_payload`` the runtime writes to its state root.
    A failed terminal raises with the node's reason and finding codes, never a Linear error text.
    """

    onex_argv: tuple[str, ...]
    lane: str = "sprint-closer-workflow"
    timeout_seconds: int = 120

    def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        request = {
            **payload,
            "correlation_id": str(uuid.uuid4()),
            "requested_by_lane": self.lane,
        }
        with tempfile.TemporaryDirectory(prefix="sprint-closer-node-") as scratch:
            source = Path(scratch) / "request.json"
            source.write_text(json.dumps(request), encoding="utf-8")
            proc = subprocess.run(
                [
                    *self.onex_argv,
                    "node",
                    NODE_ID,
                    "--input",
                    str(source),
                    "--state-root",
                    str(Path(scratch) / "state"),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            result_file = Path(scratch) / "state" / "workflow_result.json"
            if proc.returncode != 0 or not result_file.is_file():
                raise RuntimeError(
                    f"{NODE_ID} exited {proc.returncode}: {proc.stderr.strip()[-300:]}"
                )
            terminal = cast(
                dict[str, object],
                json.loads(result_file.read_text(encoding="utf-8"))["terminal_payload"],
            )
        if terminal.get("outcome") != "completed":
            raise RuntimeError(
                f"{NODE_ID} {payload.get('operation')} failed: {terminal.get('reason')} "
                f"{terminal.get('finding_codes')}"
            )
        return cast(dict[str, object], terminal["result"])


@dataclass
class HandlerLinearTicketNode:
    """Every Linear read and write of the closer, as a request to node_linear_ticket_write_effect.

    The node answers state, title, description and assignee; it has no comment history, so the closer keeps the body
    of its own last comment per ticket in ``state_dir`` and the fingerprint rule reads that.
    """

    run: ProtocolNodeRunner
    state_dir: Path

    def _request(
        self, operation: str, ticket_id: str, **fields: object
    ) -> dict[str, object]:
        digest = hashlib.sha256(
            json.dumps(fields, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return self.run(
            {
                "operation": operation,
                "ticket_id": ticket_id,
                "idempotency_key": f"{ticket_id}:{operation}:{digest}",
                **fields,
            }
        )

    def _comments(self) -> dict[str, str]:
        path = self.state_dir / STATE_FILE
        if not path.is_file():
            return {}
        return cast(dict[str, str], json.loads(path.read_text(encoding="utf-8")))

    def open_issue_ids(self, project_id: str) -> list[str]:
        out = self.run(
            {
                "operation": "list_open",
                "project": project_id,
                "idempotency_key": f"{project_id}:list_open",
            }
        )
        return sorted(str(i) for i in cast(list[object], out["candidate_ids"]))

    def sprint_projects(self) -> list[ModelTicketProjectDates]:
        out = self.run(
            {
                "operation": "list_sprint_projects",
                "idempotency_key": "list_sprint_projects",
            }
        )
        return [
            ModelTicketProjectDates.model_validate(project)
            for project in cast(list[object], out["sprint_projects"])
        ]

    def read_issue(
        self, ticket_id: str, *, include_attachments: bool = False
    ) -> LinearIssue:
        fields = {"include_attachments": True} if include_attachments else {}
        out = self._request("read", ticket_id, **fields)
        urls = out.get("attachment_urls")
        if include_attachments and not isinstance(urls, list):
            raise RuntimeError(
                f"Linear node returned no attachment reading for {ticket_id}"
            )
        last = self._comments().get(ticket_id)
        return LinearIssue(
            identifier=str(out["ticket_id"]),
            title=str(out.get("title") or ""),
            description=str(out.get("description_body") or ""),
            state=str(out.get("ticket_status") or ""),
            assignee=str(out["assignee"]) if out.get("assignee") else None,
            comment_bodies=(last,) if last else (),
            attachment_urls=tuple(str(url) for url in urls)
            if isinstance(urls, list)
            else (),
        )

    def comment(self, ticket_id: str, body: str) -> None:
        self._request("comment", ticket_id, body=body)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        comments = self._comments()
        comments[ticket_id] = body
        (self.state_dir / STATE_FILE).write_text(
            json.dumps(comments, sort_keys=True), encoding="utf-8"
        )

    def set_done(self, ticket_id: str) -> None:
        self._request("transition", ticket_id, state="Done")


@dataclass
class HandlerDodVerifyCall:
    """Runs node_dod_verify as a process, first from the registry root, then from the contract's own clone."""

    home_root: Path
    python: str = sys.executable
    extra_pythonpath: str | None = None
    timeout_seconds: int = 900
    check_timeout_seconds: int = 180

    def _run(
        self, ticket_id: str, cwd: Path, timeout_seconds: float
    ) -> tuple[int, dict[str, object] | None, str]:
        env = scrub_git_location_env(os.environ)
        # The closer owns its decision journal. Read the verifier's status from stdout.
        env.pop("ONEX_EVIDENCE_ROOT", None)
        env["DOD_VERIFY_CHECK_TIMEOUT_S"] = str(self.check_timeout_seconds)
        if self.extra_pythonpath:
            env["PYTHONPATH"] = self.extra_pythonpath
        proc = subprocess.run(
            [
                self.python,
                "-m",
                "omnimarket.nodes.node_dod_verify",
                "--ticket-id",
                ticket_id,
                "--execution-audience",
                "local_done_gate",
                "--dry-run",
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        try:
            state = cast(dict[str, object], json.loads(proc.stdout))
        except json.JSONDecodeError:
            state = None
        return (
            proc.returncode,
            state,
            (proc.stderr or "").strip().splitlines()[-1:][0]
            if proc.stderr.strip()
            else "",
        )

    def run(
        self, ticket_id: str
    ) -> tuple[str | None, str | None, tuple[ModelCheckResult, ...]]:
        deadline = monotonic() + self.timeout_seconds
        rc, state, err = self._run(ticket_id, self.home_root, self.timeout_seconds)
        if state is None:
            return None, (err or f"dod_verify exited {rc} with no result")[:300], ()
        repo = str(state.get("contract_repository") or "")
        clone = self.home_root / repo.split("/")[-1] if repo else None
        # Missing contracts already have a terminal reading. A second invocation in
        # the owning clone shares the remaining budget and must produce its own state.
        if clone is not None and clone.is_dir() and clone != self.home_root:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return (
                    None,
                    "dod_verify exhausted the ticket budget before reading its contract clone",
                    (),
                )
            clone_rc, clone_state, clone_error = self._run(ticket_id, clone, remaining)
            if clone_state is None:
                return (
                    None,
                    (
                        clone_error
                        or f"dod_verify exited {clone_rc} in its contract clone"
                    )[:300],
                    (),
                )
            state = clone_state
        return map_dod_checks(state)


@dataclass
class LiveSprintCloserSources:
    """The production sources: Linear for tickets and writes, dod_verify for checks, the ledger for readings."""

    linear: HandlerLinearTicketNode
    dod: HandlerDodVerifyCall
    ledger_read_argv: tuple[str, ...] = ()
    prs: HandlerSprintPullRequests = field(
        default_factory=lambda: HandlerSprintPullRequests(HandlerScmLocalState())
    )

    def list_ticket_ids(self, plan: ModelSprintCloserPlan) -> list[str]:
        project_id = current_project(
            self.linear.sprint_projects(), datetime.now(UTC).date()
        )
        members = self.linear.open_issue_ids(project_id)
        requested = set(plan.ticket_ids)
        return list(
            dict.fromkeys(
                ticket_id
                for ticket_id in members
                if not requested or ticket_id in requested
            )
        )

    def run_ticket(
        self, plan: ModelSprintCloserPlan, ticket_id: str
    ) -> ModelTicketOutcome:
        issue = self.linear.read_issue(ticket_id, include_attachments=True)
        name = issue.assignee
        criteria, ticked = parse_criteria(issue.description)
        bodies = list(issue.comment_bodies)  # newest first
        if issue.closed:
            return ModelTicketOutcome(
                ticket_id=ticket_id,
                assignee=name,
                verdict="leave",
                action=EnumCloserAction.NO_CHANGE,
                missing=(f"already {issue.state}",),
                dry_run=plan.dry_run,
            )
        contract_ref, contract_error, checks = self.dod.run(ticket_id)
        if not checks and contract_error is None:
            contract_error = "dod_verify reported no checks"
        pr_evidence = self.prs.read(pull_request_links(issue.attachment_urls))
        evidence = ModelTicketEvidence(
            ticket_id=ticket_id,
            title=issue.title,
            state=issue.state,
            assignee=name,
            is_collaborator=name is not None and name not in plan.owner_assignees,
            criteria=criteria,
            criteria_ticked=ticked,
            contract_ref=contract_ref,
            contract_error=contract_error,
            checks=checks,
            linked_prs=pr_evidence.merged,
            pr_blockers=pr_evidence.blockers,
            change_kind=pr_evidence.change_kind,
            seen_running=(
                ()
                if pr_evidence.change_kind is EnumChangeKind.NON_RUNTIME
                else read_seen_running(ticket_id, self.ledger_read_argv)
            ),
        )
        decision = HandlerSprintCloser(plan.actor, last_fingerprint(bodies)).handle(
            evidence
        )
        commented = False
        state_set: str | None = None
        if not plan.dry_run and decision.comment:
            self.linear.comment(ticket_id, decision.comment)
            commented = True
        if not plan.dry_run and decision.action is EnumCloserAction.SET_DONE:
            self.linear.set_done(ticket_id)
            state_set = "Done"
        return ModelTicketOutcome(
            ticket_id=ticket_id,
            assignee=name,
            verdict=decision.eligibility.verdict,
            action=decision.action,
            missing=decision.eligibility.missing,
            evidence=decision.eligibility.evidence,
            commented=commented,
            state_set=state_set,
            dry_run=plan.dry_run,
        )


def live_sources_from_env(home_root: Path) -> LiveSprintCloserSources:
    declared = os.environ.get("ONEX_SPRINT_CLOSER_LEDGER_READ_ARGV", "").strip()
    onex = os.environ.get("ONEX_SPRINT_CLOSER_ONEX") or str(
        Path(sys.executable).with_name("onex")
    )
    state_dir = Path(
        os.environ.get(
            "ONEX_SPRINT_CLOSER_STATE_DIR",
            str(home_root / ".onex_state" / "sprint-closer"),
        )
    )
    return LiveSprintCloserSources(
        linear=HandlerLinearTicketNode(HandlerOnexNodeCall((onex,)), state_dir),
        dod=HandlerDodVerifyCall(
            home_root=home_root,
            python=os.environ.get("ONEX_SPRINT_CLOSER_DOD_PYTHON", sys.executable),
            extra_pythonpath=os.environ.get("ONEX_SPRINT_CLOSER_DOD_PYTHONPATH"),
        ),
        ledger_read_argv=tuple(str(a) for a in json.loads(declared))
        if declared
        else (),
    )
