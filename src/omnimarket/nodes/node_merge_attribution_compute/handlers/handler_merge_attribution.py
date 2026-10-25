# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read attribution without turning a failed worker or a host identity into an engine."""

import re

from omnimarket.models.model_execution_identity import commit_host, engine_model

from ..models.model_merge_attribution import (
    ModelAttributionLedgerRow,
    ModelMergeAttributionRequest,
    ModelMergeAttributionResult,
)

_PR = re.compile(r"(?:[\w.-]+/)?([\w.-]+#[0-9]+)")
_PUSHED = frozenset({"merged", "fix_submitted", "cause_fix_submitted"})


def _short(pr: str) -> str:
    return pr.rsplit("/", 1)[-1]


def _names(row: ModelAttributionLedgerRow, pr: str) -> bool:
    # A diagnosis, ownership sweep or parent roll-up can mention many PRs it
    # never changed. Text alone needs a working tree as evidence of actual work.
    scope = ("pr", "handed-off", "prs", "pr_open")
    text = (
        row.text
        if not any(row.cells.get(key) for key in scope)
        and row.cells.get("worktree") not in {None, "", "none"}
        and re.search(
            r"\b(opened|pushed|created|submitted|handed off)\b", row.text, re.I
        )
        else ""
    )
    return any(
        pr == match[1]
        for value in [
            *(row.cells.get(key, "") for key in scope),
            text,
        ]
        for match in _PR.finditer(value)
    )


def _engine(cells: dict[str, str]) -> str | None:
    return next(
        (
            engine
            for key in ("engine", "model", "actor")
            if (engine := engine_model(cells.get(key, "")))
        ),
        None,
    )


class HandlerMergeAttribution:
    def handle(
        self, request: ModelMergeAttributionRequest
    ) -> ModelMergeAttributionResult:
        pr = _short(request.pr)
        host = commit_host(request.author, request.host_identity_prefix) or commit_host(
            request.committer, request.host_identity_prefix
        )
        rows = sorted(
            (
                r
                for r in request.rows
                if r.ts <= request.merged_at and r.cells.get("src") != "delegation"
            ),
            key=lambda r: r.ts,
        )
        terminals = [r for r in rows if r.kind == "TERMINAL" and _names(r, pr)]
        candidates: list[tuple[str, str, str | None, str, str, str]] = []
        for row in terminals:
            lane = row.cells.get("lane", "")
            is_worker = lane.startswith(("landing-L", "landing-cause-L"))
            if row.cells.get("outcome") in {
                "failed",
                "blocked",
                "external_blocker",
                "skipped-owned",
                "unknown",
            }:
                continue
            if is_worker and row.cells.get("result") not in _PUSHED:
                continue
            engine = _engine(row.cells)
            row_host = row.cells.get("host")
            # Resolve this attempt's CLAIM, never a later attempt or the first
            # nonempty model ever seen under a reused lane name.
            claim_ts = row.cells.get("closes-CLAIM") or row.cells.get("closes-claim")
            claims = [
                r
                for r in rows
                if r.kind == "CLAIM"
                and r.ts <= row.ts
                and r.cells.get("lane") == lane
                and (not claim_ts or r.ts == claim_ts)
            ]
            if claims:
                engine = engine or _engine(claims[-1].cells)
                row_host = row_host or claims[-1].cells.get("host")
            if engine:
                candidates.append(
                    (
                        row.ts,
                        engine,
                        row_host,
                        lane,
                        "landing_worker" if is_worker else "missing_model",
                        "terminal",
                    )
                )
        for worker in request.workers:
            subject = (
                worker.fix_ref
                if worker.result == "cause_fix_submitted"
                else worker.subject
            )
            if (
                not subject
                or _short(subject) != pr
                or worker.released_at > request.merged_at
                or worker.result not in _PUSHED
            ):
                continue
            engine = engine_model(worker.engine)
            if engine:
                candidates.append(
                    (
                        worker.released_at,
                        engine,
                        worker.host,
                        f"landing-L{worker.lease_id}",
                        "landing_worker",
                        "worker-result+cost",
                    )
                )
        # Preserve the report's handoff evidence and separately label its weaker
        # CLAIM-only fallback. Failed landing workers never qualify as a touch.
        touches: list[tuple[str, str, str | None, str, str, str]] = []
        for row in rows:
            if not _names(row, pr):
                continue
            handoff = row.kind == "MSG" and row.cells.get("to") == "landing-controller"
            if row.kind != "CLAIM" and not handoff:
                continue
            lane = row.cells.get("from" if handoff else "lane", "")
            if lane.startswith(("landing-L", "landing-cause-L")):
                continue
            claims = [
                r
                for r in rows
                if r.kind == "CLAIM" and r.ts <= row.ts and r.cells.get("lane") == lane
            ]
            engine = _engine(row.cells)
            row_host = row.cells.get("host")
            if claims:
                engine = engine or _engine(claims[-1].cells)
                row_host = row_host or claims[-1].cells.get("host")
            if engine:
                event = (
                    row.ts,
                    engine,
                    row_host,
                    lane,
                    "missing_model" if terminals else "no_terminal",
                    "handoff" if handoff else "claim-touch",
                )
                (candidates if handoff else touches).append(event)
        if not candidates:
            candidates.extend(touches)
        if candidates:
            stamp, engine, row_host, lane, reason, basis = max(
                candidates, key=lambda c: c[0]
            )
            return ModelMergeAttributionResult(
                engine=engine,
                host=row_host or host,
                lane=lane,
                reason=reason,
                basis=basis,
                evidence=(stamp,),
            )
        trailer_engines = {
            engine
            for line in request.message.splitlines()
            if re.match(r"^co-authored-by:", line, re.I)
            and not commit_host(line.partition(":")[2], request.host_identity_prefix)
            and (engine := engine_model(line.partition(":")[2].split("<", 1)[0]))
        }
        # An unspecified Claude trailer adds no contradictory model evidence.
        if len(trailer_engines) > 1:
            trailer_engines.discard("claude")
        if len(trailer_engines) == 1:
            return ModelMergeAttributionResult(
                engine=trailer_engines.pop(),
                host=host,
                reason="missing_model" if terminals else "no_terminal",
                basis="commit-coauthor",
            )
        automation = (
            bool(
                re.search(
                    r"^Onex-Workflow:|^chore\(deps|^chore: release|^evidence\(",
                    request.message,
                    re.M | re.I,
                )
            )
            or bool(
                re.search(
                    r"^chore\(deps|^chore: release|\[bot\]|^evidence\(",
                    request.title,
                    re.I,
                )
            )
            or "dependabot[bot]" in request.author
        )
        if automation:
            reason = "automation"
        elif terminals:
            reason = "missing_model"
        elif request.author and "[bot]" not in request.author and not host:
            reason = "person"
        else:
            reason = "no_terminal"
        return ModelMergeAttributionResult(
            host=host,
            reason=reason,
            basis="commit-provenance"
            if reason in {"automation", "person"}
            else "missing-ledger-evidence",
        )
