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
- ``wip-refused``: a lane that may open a PR is new work, and the started-but-unlanded count is at the repository's or
  the fleet's limit (RULING 2026-10-30T00:03:58Z). A lane that works a named PR, or whose name carries a fix-forward
  prefix, is never refused; the limits are the ``wip_admission`` overlay's, and a host with no overlay judges nothing.

The driver and brief refusals were made by a session's own dispatch helper (RULING 2026-10-18T13:45:42Z: hand fixes become part of
the permanent process); here they hold for every route to the runner. The brief's prose is read with lane-brief's own
readers, loaded from ``lane_brief_path``, so there is one definition of the rules block and of the 3a block.
"""

from __future__ import annotations

import importlib.util
import os
import re
from functools import lru_cache
from pathlib import PurePosixPath
from types import ModuleType

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.wip_admission_brief_gate import (
    dispatch_wip_refusal,
)

from ..models.model_dispatch_brief_shape import (
    ModelBriefDestinationPolicy,
    ModelBriefDestinationRequest,
    ModelBriefDestinationResult,
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
        "launching session; creating a ticket is never refused by count. A lane that cannot mint carries "
        "`Umbrella evidence: <reason>` in the brief and adds its own requirement and "
        f"dod_evidence item to contracts/{umbrella}.yaml with a test-side path that fails at the merge base"
    )


OVERLAY_ID = "brief_destinations"
_DECLARATION = re.compile(
    r"^\s*(?:[-*]\s*)?(creates|edits|modifies|updates|deletes|removes|evidence):"
    r"\s*([A-Za-z0-9_.-]+)@[^:\s]+:(\S+)",
    re.IGNORECASE,
)
_WRITE_VERBS = frozenset(
    {
        "add",
        "create",
        "write",
        "implement",
        "edit",
        "modify",
        "update",
        "fix",
        "change",
        "extend",
        "patch",
        "refactor",
        "append",
        "touch",
    }
)
_ACTION = re.compile(
    r"(?<![\w./-])("
    + "|".join(sorted(_WRITE_VERBS))
    + r"|move|read|delete|remove|inspect|run|verify|check)(?=\s)",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\b(?:not|never|don't|do not)\s+$", re.IGNORECASE)
_REPO = re.compile(r"^\s*(?:Repo(?:sitory)?|Target repo):\s*(\S+)\s*$", re.IGNORECASE)
_QUALIFIED = re.compile(r"(?<![\w.-])([\w.-]+)@[^:\s`]+:([^\s`]+)")
_REPO_PATH = re.compile(
    r"(?<![\w./-])([\w.-]+)/((?:src|tests?|scripts|config|docs)/[^\s`]+)"
)
_RELATIVE_PATH = re.compile(r"(?<![\w./-])((?:src|tests?|scripts|config|docs)/[^\s`]+)")
_PATH_ROOTS = frozenset({"src", "tests", "test", "scripts", "config", "docs"})
_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".rb",
        ".pl",
        ".go",
        ".rs",
        ".lua",
        ".ps1",
        ".php",
    }
)


def _destinations(request: ModelBriefDestinationRequest) -> tuple[tuple[str, str], ...]:
    """Read task write targets; do not infer a write from a bare evidence citation."""
    repository = request.repository
    for line in request.authored_prose.splitlines():
        if match := _REPO.match(line):
            repository = match[1]
    targets: dict[tuple[str, str], None] = {}
    for line in request.authored_prose.splitlines():
        if line.lstrip().startswith((">", "#")):
            continue
        if declared := _DECLARATION.match(line):
            if declared[1].lower() not in {"evidence", "deletes", "removes"}:
                targets[(declared[2], declared[3].rstrip(",;)."))] = None
            continue
        actions = list(_ACTION.finditer(line))
        for index, action in enumerate(actions):
            if _NEGATED.search(line[: action.start()]):
                continue
            verb = action[1].lower()
            segment = line[
                action.end() : actions[index + 1].start()
                if index + 1 < len(actions)
                else len(line)
            ]
            if verb == "move":
                # Moving out is allowed; moving code into a frozen repository is not.
                into = re.search(r"\b(?:to|into)\s+", segment)
                if into is None:
                    continue
                segment = segment[into.end() :]
            elif verb not in _WRITE_VERBS:
                continue
            qualified = list(_QUALIFIED.finditer(segment))
            repo_paths = [
                m for m in _REPO_PATH.finditer(segment) if m[1] not in _PATH_ROOTS
            ]
            for match in (*qualified, *repo_paths):
                targets[(match[1], match[2].rstrip(",;)."))] = None
            for match in _RELATIVE_PATH.finditer(segment):
                if not any(
                    m.start() <= match.start() < m.end()
                    for m in (*qualified, *repo_paths)
                ):
                    targets[(repository, match[1].rstrip(",;)."))] = None
    return tuple(targets)


class HandlerBriefDestinations:
    def handle(
        self, request: ModelBriefDestinationRequest
    ) -> ModelBriefDestinationResult:
        targets = _destinations(request)
        if not targets:
            return ModelBriefDestinationResult(refusals=())
        try:
            policy = load_structured_overlay(OVERLAY_ID, ModelBriefDestinationPolicy)
        except NodeOverlayError as exc:
            return ModelBriefDestinationResult(
                refusals=(
                    (
                        "destination-policy-unavailable",
                        f"REMOTE-LANE-DESTINATION-POLICY-UNAVAILABLE {exc}",
                    ),
                )
            )
        refusals: list[tuple[str, str]] = []
        for repository, path in targets:
            path = path.strip("`\"'")
            parts = PurePosixPath(path).parts
            if (
                not repository
                or not parts
                or path.startswith(("/", "~"))
                or "\\" in path
                or ".." in parts
            ):
                refusals.append(
                    (
                        "destination-unreadable",
                        f"REMOTE-LANE-DESTINATION-UNREADABLE {repository}:{path}: "
                        "name the repository and a repo-relative destination without '..'",
                    )
                )
                continue
            code = (
                parts[0] in {"src", "tests", "test"}
                or PurePosixPath(path).suffix.lower() in _CODE_SUFFIXES
            )
            if repository in policy.frozen_repositories and code:
                refusals.append(
                    (
                        "frozen-destination-refused",
                        f"REMOTE-LANE-FROZEN-DESTINATION-REFUSED {repository}:{path}: "
                        "the repository is frozen; move the code and its tests to "
                        f"{policy.migration_repository} as a node or handler with its contract "
                        "before changing them, then delete the old copy; pure data may stay",
                    )
                )
        return ModelBriefDestinationResult(refusals=tuple(refusals))


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
        refusals.extend(
            HandlerBriefDestinations()
            .handle(
                ModelBriefDestinationRequest(
                    authored_prose=lane_brief.premise_text(request.brief),
                    repository=request.repo or "",
                )
            )
            .refusals
        )
        named = BRIEF_REPO_RE.search(lane_brief.premise_text(request.brief))
        if request.repo or named:
            wip = dispatch_wip_refusal(
                lane=request.lane,
                repo=request.repo or "",
                ticket=request.ticket or "",
                prs=tuple(request.prs),
                environ=os.environ,
            )
            if wip:
                refusals.append(("wip-refused", wip))
        return ModelDispatchBriefShapeResult(tuple(refusals))
