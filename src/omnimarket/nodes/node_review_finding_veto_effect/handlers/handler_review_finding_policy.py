# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for the review finding policy of node_review_finding_veto_effect (OMN-17427).

A blocking finding blocks only when it is an Important finding the pull request
introduced and it carries evidence. Four rules, each checked against git rather
than against what a voter says:

1. Classification. A flagged line the pull request did not add or change exists
   at the base: the finding is Pre-existing and cannot block this pull request.
   A finding of a style or cleanup class is a Nit and cannot block either.
2. Evidence. A finding with no file and line inside the pull request's diff is
   refused: the file is not one the pull request changed, or the line is not in
   the file at the head.
3. Style and cleanup classes never block (rule 1's Nit). Real defects are
   protected: a security, logic, integration, contract or scope category, a
   title that names a defect, and a finding bound to a standing rule are never
   read as a Nit, and a standing rule is never judged by line.
4. Rounds. From the fourth review of one pull request the gate re-reviews only
   what changed since the previous round and never repeats a finding it has
   already posted.

A drop is logged with its class and reason, and nothing is dropped when the diff
cannot be read.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from typing import Any

from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_diff import (
    GitDiffReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_head import (
    GitHeadFileReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.models import (
    EnumFindingDisposition,
    ModelFindingDrop,
    ModelReviewFindingPolicyRequest,
    ModelReviewFindingPolicyResult,
    ModelReviewRoundState,
)
from omnimarket.nodes.node_review_finding_veto_effect.protocols import (
    ProtocolDiffReader,
    ProtocolHeadFileReader,
)

_log = logging.getLogger(__name__)

ROUND_LIMIT = 3
_TITLE_SEPARATOR = " | "

# Rules the reviewer binds to the repository's own standing rules, or to its rules file.
_EXEMPT_RULE_PREFIXES = ("standing:", "rules-file:")

_STYLE_CATEGORIES = frozenset({"style", "informational"})
_DEFECT_CATEGORIES = frozenset(
    {"security", "logic_error", "integration", "contract_breach", "scope_violation"}
)

# A title that names a style or cleanup change, not a defect.
_CLEANUP_TITLE = re.compile(
    r"\bunused\s+(?:import|variable|argument|parameter|local)"
    r"|\b(?:naming|rename|renaming)\b"
    r"|\bdocstrings?\b"
    r"|\btypos?\b"
    r"|\bformatting\b|\bwhitespace\b|\bindentation\b|\bline\s+(?:length|too\s+long)\b"
    r"|\bimport\s+(?:order|sorting)\b"
    r"|\blint(?:er|ing)?\b|\bdiagnostics?\b"
    r"|\bmissing\s+(?:type\s+)?(?:hints?|annotations?)\b"
    r"|\bmagic\s+numbers?\b"
    r"|\bdead\s+code\b|\bcommented[- ]out\b"
    r"|\bredundant\b|\breadability\b|\brefactor"
    r"|\bconsider\s+(?:using|renaming|extracting|simplif)"
    r"|\bcould\s+be\s+(?:simplified|extracted|renamed)\b|\bcleanup\b|\bclean[- ]up\b",
    re.IGNORECASE,
)

# A title that names a defect keeps a finding out of the Nit class.
_DEFECT_TITLE = re.compile(
    r"\bbug\b|\bcrash|\bincorrect|\bwrong\b|\bbreaks?\b|\bbroken\b|\bdata\s+loss\b"
    r"|\brace\b|\binjection\b|\bvulnerab|\bleak|\bdeadlock|\bsilent(?:ly)?\b"
    r"|\bbypass|\bunsafe\b|\bsecret|\bcredential|\bremov(?:es|ed|al)\b"
    r"|\bskip|\bfail(?:s|ure)?\b|\bregress|\bnever\b|\balways\s+(?:true|false)\b",
    re.IGNORECASE,
)

LineRanges = dict[str, tuple[tuple[int, int], ...]]


def _title(message: str) -> str:
    return message.split(_TITLE_SEPARATOR, 1)[0]


def _target_path(file_path: str) -> str:
    """The reviewer writes a model's free-text location into the path field."""
    return file_path.split(":", 1)[0].strip()


def fingerprint(file_path: str, rule: str, message: str) -> str:
    """Identity of a finding across rounds: its file, class and title, not its line."""
    title = re.sub(r"\W+", " ", _title(message).lower()).strip()
    key = f"{_target_path(file_path).lower()}|{rule.strip().lower()}|{title}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _category(rule: str) -> str | None:
    return rule.split(":", 1)[1] if rule.startswith("category:") else None


def _in(ranges: tuple[tuple[int, int], ...], line: int) -> bool:
    return any(low <= line <= high for low, high in ranges)


def _resolve(path: str, changed: LineRanges) -> str | None:
    """The changed file a reviewer's (possibly lower-cased) path names, if one."""
    if not path:
        return None
    if path in changed:
        return path
    matches = [name for name in changed if name.lower() == path.lower()]
    return matches[0] if len(matches) == 1 else None


class HandlerReviewFindingPolicy:
    """EFFECT: lets only introduced, evidenced, non-style findings stay blocking."""

    def __init__(
        self,
        files: ProtocolHeadFileReader | None = None,
        diffs: ProtocolDiffReader | None = None,
    ) -> None:
        self._files = files or GitHeadFileReader(".")
        self._diffs = diffs or GitDiffReader(".")

    def handle(
        self, request: ModelReviewFindingPolicyRequest
    ) -> ModelReviewFindingPolicyResult:
        document = copy.deepcopy(request.result)
        prior = request.prior_state
        round_number, previous_head, posted_before = _round_of(prior, request.head_sha)

        quorum = document.get("quorum")
        clusters = quorum.get("blocking_findings") if isinstance(quorum, dict) else None
        if not isinstance(quorum, dict) or not isinstance(clusters, list):
            return self._result(
                document,
                (),
                [],
                round_number,
                request.head_sha,
                previous_head,
                posted_before,
            )

        introduced = self._diffs.changed_lines(
            request.base_sha, request.head_sha, from_merge_base=True
        )
        if introduced is None:
            _log.warning("review finding policy: the pull request diff is unreadable")
            return self._result(
                document,
                (),
                clusters,
                round_number,
                request.head_sha,
                previous_head,
                posted_before,
            )
        incremental: LineRanges | None = None
        if round_number > ROUND_LIMIT and previous_head is not None:
            incremental = self._diffs.changed_lines(
                previous_head, request.head_sha, from_merge_base=False
            )
            if incremental is None:
                _log.warning(
                    "review finding policy: the diff since %s is unreadable; only "
                    "posted findings are suppressed",
                    previous_head,
                )

        kept: list[Any] = []
        dropped: list[ModelFindingDrop] = []
        for cluster in clusters:
            drop = self._judge(
                cluster,
                request.head_sha,
                introduced,
                incremental,
                posted_before if round_number > ROUND_LIMIT else frozenset(),
                round_number > ROUND_LIMIT,
            )
            if drop is None:
                kept.append(cluster)
                continue
            dropped.append(drop)
            _log.info("review finding dropped (%s): %s", drop.disposition, drop.reason)

        if dropped:
            quorum["blocking_findings"] = kept
            quorum["blocking_count"] = len(kept)
            quorum["policy_dropped"] = [d.model_dump(mode="json") for d in dropped]
            if not kept and quorum.get("verdict") == "blocked":
                quorum["verdict"] = "passed"
        quorum["review_round"] = round_number
        return self._result(
            document,
            tuple(dropped),
            kept,
            round_number,
            request.head_sha,
            previous_head,
            posted_before,
        )

    @staticmethod
    def _result(
        document: dict[str, Any],
        dropped: tuple[ModelFindingDrop, ...],
        kept: list[Any],
        round_number: int,
        head_sha: str,
        previous_head: str | None,
        posted_before: frozenset[str],
    ) -> ModelReviewFindingPolicyResult:
        shown = [c for c in kept if isinstance(c, dict)]
        posted = posted_before | {
            fingerprint(
                str(c.get("file_path", "")),
                str(c.get("rule", "")),
                str(c.get("message", "")),
            )
            for c in shown
        }
        return ModelReviewFindingPolicyResult(
            result=document,
            dropped=dropped,
            kept=len(kept),
            state=ModelReviewRoundState(
                round=round_number,
                head=head_sha,
                previous_head=previous_head,
                posted=tuple(sorted(posted)),
                posted_before=tuple(sorted(posted_before)),
            ),
            kept_messages=tuple(
                f"{_target_path(str(c.get('file_path', '')))}:{c.get('line_start', 1)} "
                f"{_title(str(c.get('message', '')))[:160]}"
                for c in shown
            ),
        )

    def _judge(
        self,
        cluster: Any,
        head_sha: str,
        introduced: LineRanges,
        incremental: LineRanges | None,
        posted_before: frozenset[str],
        past_round_limit: bool,
    ) -> ModelFindingDrop | None:
        if not isinstance(cluster, dict):
            return None
        rule = str(cluster.get("rule", ""))
        if rule.startswith(_EXEMPT_RULE_PREFIXES):
            return None
        message = str(cluster.get("message", ""))
        title = _title(message)
        path = _target_path(str(cluster.get("file_path", "")))
        raw_line = cluster.get("line_start")
        line = (
            raw_line
            if isinstance(raw_line, int) and not isinstance(raw_line, bool)
            else 0
        )
        fp = fingerprint(str(cluster.get("file_path", "")), rule, message)
        label = f"{path}:{line} {title[:80]}"

        def drop(disposition: EnumFindingDisposition, reason: str) -> ModelFindingDrop:
            return ModelFindingDrop(
                file_path=path,
                line_start=line,
                disposition=disposition,
                reason=f"{label}: {reason}",
                fingerprint=fp,
                message=message,
            )

        style = _style_reason(rule, title)
        if style is not None:
            return drop(EnumFindingDisposition.NIT, style)

        resolved = _resolve(path, introduced)
        if resolved is None:
            return drop(
                EnumFindingDisposition.NO_EVIDENCE,
                "no file inside the diff: "
                + (
                    "the finding names no file"
                    if not path
                    else "the pull request does not change that file"
                ),
            )
        if line < 1:
            return drop(EnumFindingDisposition.NO_EVIDENCE, "no line is given")
        text = self._files.read(head_sha, resolved)
        if text is not None and line > len(text.splitlines()):
            return drop(
                EnumFindingDisposition.NO_EVIDENCE,
                f"line {line} is past the end of the file at the head "
                f"({len(text.splitlines())} lines)",
            )
        if not _in(introduced[resolved], line):
            return drop(
                EnumFindingDisposition.PRE_EXISTING,
                f"line {line} is not added or changed by the pull request; it exists at the base",
            )

        if past_round_limit:
            if fp in posted_before:
                return drop(
                    EnumFindingDisposition.REPEAT,
                    f"already posted in an earlier round (past round {ROUND_LIMIT})",
                )
            if incremental is not None:
                inc = _resolve(resolved, incremental)
                if inc is None or not _in(incremental[inc], line):
                    return drop(
                        EnumFindingDisposition.ALREADY_REVIEWED,
                        f"line {line} is not in the diff since the previous round "
                        f"(past round {ROUND_LIMIT}: only the new diff is re-reviewed)",
                    )
        return None


def _round_of(
    prior: ModelReviewRoundState | None, head_sha: str
) -> tuple[int, str | None, frozenset[str]]:
    """This run's round number, the head of the round before it, and what was posted before."""
    if prior is None:
        return 1, None, frozenset()
    if prior.head == head_sha:
        return prior.round, prior.previous_head, frozenset(prior.posted_before)
    return prior.round + 1, prior.head, frozenset(prior.posted)


def _style_reason(rule: str, title: str) -> str | None:
    """Why the finding is a Nit, or None when it may block."""
    category = _category(rule)
    if category in _STYLE_CATEGORIES:
        return f"category {category} is a style class and never blocks"
    if category in _DEFECT_CATEGORIES or _DEFECT_TITLE.search(title):
        return None
    if _CLEANUP_TITLE.search(title):
        return "the title names a style or cleanup change, which never blocks"
    return None


__all__: list[str] = ["ROUND_LIMIT", "HandlerReviewFindingPolicy", "fingerprint"]
