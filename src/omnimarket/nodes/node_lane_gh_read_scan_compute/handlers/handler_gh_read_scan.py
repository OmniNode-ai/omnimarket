# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name every line of a lane-facing text that tells a lane to run a GitHub call through gh (OMN-18946).

RULING 2026-10-10T18:17:36Z: lanes do not read GitHub; gh is for writes, and a lane's writes go through
``onex scm``. The real-gh guard (OMN-22539) refuses a lane's ``gh`` call at run time: 101 refusals in the 24 hours
to 2026-11-09T06:46Z, 92 of them from orchestrator subagents following a brief or a skill, 9 from remote lanes.
This reader refuses the line that would cause one, before a lane reads it: a dispatch brief (the brief-shape
refusal ``gh-read-refused``), and a skill, a script or a prompt template (the whole-tree test of each repository).

A line is a finding when it names a ``gh`` read (``gh pr view``, ``gh run view``, ``gh api`` GET, the same verb as
argv words, the GitHub API addressed by curl) or a ``gh`` write that has an ``onex scm`` verb (``gh pr create``),
and is neither negated (``never``, ``refused``, ``instead of``), nor quoted history (a ``>`` line), nor pasted
output (a ``text`` fence), nor the exact-head declaration the gh shim allows (``ONEX_GH_EXACT_HEAD``). Each
finding names the mirror that answers the same question.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_gh_read_scan import (
    ModelGhReadFinding,
    ModelGhReadScanRequest,
    ModelGhReadScanResult,
    ModelGhReadSurface,
)

_PR_STATE = (
    'python3 "${OMNI_PLUGIN_ROOT}"/skills/merge-drain/scripts/pr_state_local.py --pr <repo>#<n>, '
    "or onex scm pr-view|pr-status|pr-merged|pr-merge-commit <owner>/<repo>#<n>"
)

# (shape, pattern, mirror). The first shape that matches a span names the finding; a span is reported once.
SHAPES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("gh pr view", re.compile(r"\bgh\s+pr\s+view\b"), _PR_STATE),
    (
        "gh pr checks",
        re.compile(r"\bgh\s+pr\s+(?:checks|status)\b"),
        "onex scm pr-checks <owner>/<repo>#<n>",
    ),
    (
        "gh pr list",
        re.compile(r"\bgh\s+pr\s+list\b"),
        "onex scm pr-list <owner>/<repo>, or pr_state_local.py --find <ticket>",
    ),
    (
        "gh pr diff",
        re.compile(r"\bgh\s+pr\s+(?:diff|checkout)\b"),
        'git -C "$OMNI_HOME/<repo>" diff origin/<base>...origin/<head_ref> (after canonical_clone_sync.py refresh)',
    ),
    (
        "gh run",
        re.compile(r"\bgh\s+run\s+(?:view|list|watch|download)\b"),
        "onex scm run-view|run-list|run-download <owner>/<repo>, or onex scm job-log <owner>/<repo> --job-id <id>",
    ),
    (
        "gh repo",
        re.compile(r"\bgh\s+repo\s+(?:view|list)\b"),
        'git -C "$OMNI_HOME/<repo>" symbolic-ref --short refs/remotes/origin/HEAD, '
        "or pr_state_local.py --settings <repo>",
    ),
    (
        "gh issue",
        re.compile(r"\bgh\s+issue\s+(?:view|list|status)\b"),
        "the ticket tracker's own read (get_issue); a pull request's text through onex scm pr-view",
    ),
    (
        "gh search",
        re.compile(r"\bgh\s+search\b"),
        "pr_state_local.py --find <ticket>, or git log on the canonical clone",
    ),
    (
        "gh release/workflow/variable read",
        re.compile(
            r"\bgh\s+(?:release\s+(?:list|view|download)|workflow\s+(?:list|view)|variable\s+get)\b"
        ),
        "onex scm release-list <owner>/<repo>, or pr_state_local.py --settings <repo>[:<branch>]",
    ),
    (
        "gh pr write",
        re.compile(r"\bgh\s+pr\s+(?:create|comment|edit|ready|close|reopen)\b"),
        "onex scm pr-create|pr-comment|pr-edit|pr-ready|pr-close (onex scm verbs prints every verb)",
    ),
    (
        "gh workflow run",
        re.compile(r"\bgh\s+workflow\s+run\b"),
        "onex scm workflow-run <owner>/<repo>",
    ),
    (
        "GitHub API over http",
        re.compile(
            r"api\.github\.com|raw\.githubusercontent\.com|codeload\.github\.com"
        ),
        'git -C "$OMNI_HOME/<repo>" show origin/<base>:<path>, or onex scm file-read',
    ),
)
GH_API = re.compile(r"""\bgh\s+api\b""")
GH_API_WRITE = re.compile(
    r"""(?:-X|--method)[\s=]*(?:POST|PATCH|PUT|DELETE)\b|(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?=[\s=])"""
)
GH_API_EXEMPT = re.compile(r"--help\b|\brate_limit\b|\bmutation\b")
GH_API_MIRROR = (
    "onex scm commit-check-runs|pr-checks|job-log|branch-protection|file-read|commit-view|pr-commits "
    "<owner>/<repo>, or pr_state_local.py --settings <repo>[:<branch>]"
)
GH_API_WRITE_MIRROR = "onex scm pr-create|pr-comment|pr-thread-reply|pr-review-post|check-run-create <owner>/<repo>"
EXACT_HEAD = "ONEX_GH_EXACT_HEAD"
ARGV_JOIN = re.compile(r"""["']\s*,\s*["']""")
COMMENT_PREFIXES = ("#", "//", "*")
PROHIBITION_BEFORE = re.compile(
    r"\b(?:never|not|no|nor|without|refus\w*|forbid\w*|instead of|in place of|rather than|replaces?|replaced"
    r"|was|were|don't|do not|bars?|barred|stop|stops)\b",
    re.IGNORECASE,
)
PROHIBITION_AFTER = re.compile(
    r"^\W{0,3}(?:is|are|was|were|and\b.{0,40}\b(?:is|are))?\s*(?:refus|forbid|barred|denied)",
    re.IGNORECASE,
)
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")
PASTED_OUTPUT_FENCES = frozenset({"text", "txt", "log", "json", "jsonl"})


def _prohibited(line: str, start: int, end: int, previous: str) -> bool:
    before = (previous + " " + line)[
        max(0, len(previous) + 1 + start - 90) : len(previous) + 1 + start
    ]
    before = re.split(r"[.;:]\s", before)[-1]
    return bool(
        PROHIBITION_BEFORE.search(before)
        or PROHIBITION_AFTER.match(line[end : end + 60].lstrip("`)\" '"))
    )


def _lines(surface: ModelGhReadSurface) -> list[tuple[int, str]]:
    """The (1-based line, text) pairs a lane could run or follow."""
    out: list[tuple[int, str]] = []
    fence = ""
    lang = ""
    for number, line in enumerate(surface.text.splitlines(), 1):
        opening = FENCE_OPEN.match(line)
        if fence:
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line
            ):
                fence = ""
            elif lang not in PASTED_OUTPUT_FENCES:
                out.append((number, line))
        elif opening and not (opening[1][0] == "`" and "`" in line[opening.end(1) :]):
            fence, lang = opening[1], opening[2].lower()
        else:
            out.append((number, line))
    kept: list[tuple[int, str]] = []
    for number, line in out:
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue
        if surface.kind == "code" and stripped.startswith(COMMENT_PREFIXES):
            continue
        kept.append((number, ARGV_JOIN.sub(" ", line)))
    return kept


def _spans(line: str) -> list[tuple[int, int, str, str]]:
    found: list[tuple[int, int, str, str]] = []
    for shape, regex, mirror in SHAPES:
        found.extend((m.start(), m.end(), shape, mirror) for m in regex.finditer(line))
    for m in GH_API.finditer(line):
        tail = line[m.end() :]
        if GH_API_EXEMPT.search(tail):
            continue
        if GH_API_WRITE.search(tail):
            found.append((m.start(), m.end(), "gh api write", GH_API_WRITE_MIRROR))
        else:
            found.append((m.start(), m.end(), "gh api GET", GH_API_MIRROR))
    return sorted(found)


class HandlerGhReadScan:
    def handle(self, request: ModelGhReadScanRequest) -> ModelGhReadScanResult:
        findings: list[ModelGhReadFinding] = []
        for surface in request.surfaces:
            lines = _lines(surface)
            for index, (number, line) in enumerate(lines):
                if EXACT_HEAD in line:
                    continue
                previous = (
                    lines[index - 1][1]
                    if index and lines[index - 1][0] == number - 1
                    else ""
                )
                for start, end, shape, mirror in _spans(line):
                    if _prohibited(line, start, end, previous):
                        continue
                    findings.append(
                        ModelGhReadFinding(
                            path=surface.path,
                            line=number,
                            shape=shape,
                            command=line[start:end].strip(),
                            mirror=mirror,
                        )
                    )
        return ModelGhReadScanResult(findings=tuple(findings))


CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_BUS = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["event_bus"]
COMPLETED_TOPIC = next(t for t in _BUS["publish_topics"] if t.endswith("-completed.v1"))
FAILED_TOPIC = next(t for t in _BUS["publish_topics"] if t.endswith("-failed.v1"))


def answer_command(
    payload: bytes | str, handler: HandlerGhReadScan
) -> list[tuple[str, bytes]]:
    """The events one ``check_gh_reads`` command produces: one completed event, or one typed failure for a bad payload."""
    try:
        request = ModelGhReadScanRequest.model_validate_json(payload)
    except ValidationError as exc:
        try:
            correlation = str(json.loads(payload).get("correlation_id", ""))
        except (ValueError, AttributeError):
            correlation = ""
        failure: dict[str, object] = {
            "correlation_id": correlation,
            "failure": "bad-payload",
            "detail": exc.errors(include_url=False, include_context=False)[0]["msg"],
        }
        return [(FAILED_TOPIC, json.dumps(failure).encode())]
    result = handler.handle(request)
    body: dict[str, object] = {
        "correlation_id": request.correlation_id,
        "refused": bool(result.findings),
        "findings": [f.model_dump() for f in result.findings],
    }
    return [(COMPLETED_TOPIC, json.dumps(body).encode())]
