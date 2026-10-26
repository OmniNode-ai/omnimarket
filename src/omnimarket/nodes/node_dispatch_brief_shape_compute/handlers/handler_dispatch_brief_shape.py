# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote-lane runner's pre-placement refusals of a brief or a lane name (OMN-21021, OMN-17427).

Each refusal comes before placement, remote provisioning and CLAIM, with exit 2 in the runner:

- ``mint-refused``: a lab-bound brief grants or asks for a ticket mint (lane-brief's ``lab_mint_problems``, OMN-21021).
- ``brief-refused``: the brief pastes the standing rules block but has no task line of its own outside it, the 3a
  block, fenced examples, headings and the ROUTE and Parent lines.
- ``driver-no-pr``: a driver lane (``pf-drive-<repo>-<n>``, the lane loops_check's process-fix FIX names; a session
  adds ``-<HHMM>``) names no PR target, so its CLAIM would own no PR, pr-handoff would refuse it no-claim and a Codex
  driver would have no PR head to fetch.

- ``umbrella-refused``: a lane that may open a PR (a repo, or the contract-before-push step) names only an umbrella ticket
  (OMN-17427 today, 154 criteria), so its PR title would share the umbrella's one contract and spend up to 40 minutes in
  dod-verify (OMN-21301). The fix is a child ticket; a brief that carries ``Umbrella evidence: <reason>`` is admitted.

The driver and brief refusals were made by a session's own dispatch helper (RULING 2026-10-18T13:45:42Z: hand fixes become part of
the permanent process); here they hold for every route to the runner. The brief's prose is read with lane-brief's own
readers, loaded from ``lane_brief_path``, so there is one definition of the rules block and of the 3a block.
"""

from __future__ import annotations

import importlib.util
import re
from functools import lru_cache
from types import ModuleType

from ..models.model_dispatch_brief_shape import (
    ModelDispatchBriefShapeRequest,
    ModelDispatchBriefShapeResult,
)

METADATA_RE = re.compile(
    r"^(?:#|ROUTE:|Parent:|Lane kind:|Timeout reason:|Required capabilities:)",
    re.IGNORECASE,
)
RULES_ONLY = (
    "the brief carries the standing rules block but no task of its own (no line outside the rules block, the 3a "
    "block, fenced examples, headings and the ROUTE and Parent lines); write the lane's premises and task into "
    "the brief"
)
UMBRELLA_TICKETS = frozenset({"OMN-17427"})
UMBRELLA_EVIDENCE_RE = re.compile(
    r"^\s*Umbrella evidence:\s*(?P<reason>\S.*?)\s*$", re.IGNORECASE
)
CLAIM_TICKET_RE = re.compile(r"\| ticket=(OMN-[0-9]+) \|")
BRIEF_REPO_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:repo(?:sitory)?|target repo):\s*\S|\| repo=\S+|--repo(?:=|\s+)\S"
)
DRIVER_PREFIX = "pf-drive-"
DRIVER_RE = re.compile(
    r"^pf-drive-(?P<repo>[A-Za-z0-9_.-]+?)-(?P<number>[0-9]+)(?:-[0-9]{4})?$"
)


@lru_cache(maxsize=4)
def load_lane_brief(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dispatch_brief_shape_lane_brief", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load lane_brief from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rules_only(brief: str, lane_brief: ModuleType) -> bool:
    """True when the brief pastes the rules block and its own prose holds no task line."""
    head = lane_brief.RULES_BLOCK_HEAD
    if not re.search(
        rf"^{re.escape(head)}", lane_brief.unfenced_prose(brief), re.MULTILINE
    ):
        return False
    prose = lane_brief.premise_text(brief).splitlines()
    return not any(x.strip() and not METADATA_RE.match(x.strip()) for x in prose)


def driver_problem(lane: str, prs: tuple[str, ...]) -> str | None:
    """Why a driver lane is refused: it names no PR target (no --pr, no pr= on its brief CLAIM line)."""
    if not lane.startswith(DRIVER_PREFIX) or prs:
        return None
    m = DRIVER_RE.match(lane)
    want = f"--pr {m.group('repo')}#{m.group('number')}" if m else "--pr <repo>#<n>"
    return (
        f"driver lane {lane} names no PR target: pass {want} (or a pr= cell on the brief's CLAIM line for the lane), "
        "so its CLAIM owns the PR it carries to merged"
    )


def umbrella_evidence(brief: str, lane_brief: ModuleType) -> bool:
    """True when the brief carries an ``Umbrella evidence: <reason>`` line outside fences and quotes, with a real reason."""
    for line in lane_brief.unfenced_prose(brief).splitlines():
        found = UMBRELLA_EVIDENCE_RE.match(line)
        if found and not re.fullmatch(r"<[^>]*>", found.group("reason")):
            return True
    return False


def umbrella_problem(
    request: ModelDispatchBriefShapeRequest, lane_brief: ModuleType
) -> str | None:
    """Why a lane that may open a PR is refused: its only ticket, the dispatch's or the brief CLAIM's, is an umbrella."""
    claim_body = lane_brief.split_parts(request.brief)[0].get(
        lane_brief.H_CLAIM, ("", "")
    )[1]
    claim = CLAIM_TICKET_RE.search(claim_body)
    umbrella = next(
        (
            t
            for t in (request.ticket, claim and claim.group(1))
            if t in UMBRELLA_TICKETS
        ),
        None,
    )
    if umbrella is None or umbrella_evidence(request.brief, lane_brief):
        return None
    named = BRIEF_REPO_RE.search(
        lane_brief.premise_text(request.brief)
    ) or BRIEF_REPO_RE.search(claim_body)
    if not (
        request.repo or named or lane_brief.has_contract_step(request.brief, umbrella)
    ):
        return None  # a read-only or orchestration lane opens no PR
    return (
        f"umbrella ticket {umbrella} cannot identify a lane's PR: its one contract carries every umbrella criterion, "
        f"so each umbrella-titled PR spends up to 40 minutes in dod-verify. Fix: pass a child ticket of {umbrella} with "
        f"--ticket, either an existing open child or one minted with /omni:create-ticket (parent {umbrella}) in the "
        "launching session. When the create gate refuses with lane_intake_budget and no open child fits, carry "
        "`Umbrella evidence: lane_intake_budget` in the brief and have the lane add its own requirement and "
        f"dod_evidence item to contracts/{umbrella}.yaml with a test-side path that fails at the merge base"
    )


class HandlerDispatchBriefShape:
    def handle(
        self, request: ModelDispatchBriefShapeRequest
    ) -> ModelDispatchBriefShapeResult:
        lane_brief = load_lane_brief(request.lane_brief_path)
        refusals: list[tuple[str, str]] = []
        mint = lane_brief.lab_mint_problems(request.brief)
        if mint:
            refusals.append(
                ("mint-refused", f"REMOTE-LANE-MINT-REFUSED {'; '.join(mint)}")
            )
        if rules_only(request.brief, lane_brief):
            refusals.append(
                ("brief-refused", f"REMOTE-LANE-BRIEF-REFUSED {RULES_ONLY}")
            )
        driver = driver_problem(request.lane, tuple(request.prs))
        if driver:
            refusals.append(("driver-no-pr", f"REMOTE-LANE-DRIVER-NO-PR {driver}"))
        umbrella = umbrella_problem(request, lane_brief)
        if umbrella:
            refusals.append(
                ("umbrella-refused", f"REMOTE-LANE-UMBRELLA-REFUSED {umbrella}")
            )
        return ModelDispatchBriefShapeResult(tuple(refusals))
