# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the hourly Linear-comments sweep (OMN-20680).

A behaviour-preserving port of the pure block of the retired ``linear-comments-hourly``
workflow script. tests/fixtures/linear_comments_hourly_parity.json holds the old block's
outputs over the same inputs. Regexes run ASCII-only (``re.ASCII``) because JavaScript's
``\\b`` and ``\\w`` are ASCII-only; the one divergence is ``\\s``, which is ASCII-only here
and Unicode-wide in JavaScript.

Why the rules look the way they do (kept from the script):

* A human collaborator whose ``author_role`` *denies* automation ("not automation") is a
  human; negated clauses are stripped before markers are searched.
* A first-line ``actor: <lane> (<model>)`` attribution marks an agent comment whatever the
  account author is.
* Every refusal carries the reason the decision itself produced; nothing re-derives it.
* A draft that cites a comment id outside the source set never reaches the verifier.
* A response cut off by its token limit is never scored complete.
* Every held item lands in one member of the closed ``EnumHeldClass`` set.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from omnibase_core.types import JsonType

from omnimarket.nodes.node_linear_comments_decision_compute.models.model_linear_comments_decision import (
    EnumAuthorKind,
    EnumHeldClass,
    EnumLinearCommentsDecisionKind,
    ModelAttemptRow,
    ModelCollectedItem,
    ModelDelegateOutcome,
    ModelDraftCitationCheck,
    ModelHeldInput,
    ModelItemDecision,
    ModelLinearCommentsDecisionRequest,
    ModelLinearCommentsDecisionResult,
    ModelMetricsRow,
    ModelReplyDraft,
    ModelTruncationCheck,
)

_FLAGS = re.IGNORECASE | re.ASCII

AUTOMATION_WORDS = (
    "(?:bot|bots|app|apps|sweep|sweeps|sweeper|automation|automations|automated|"
    "orchestrator|closer|autoclose|github-actions|onexbot|linear-closer|cron|webhook|"
    "workflow|robot|integration)"
)
SELF_WORDS = (
    "(?:operator'?s own(?:\\s+\\w+){0,2}\\s+identity|under the operator'?s(?:\\s+own)? identity|"
    "as the operator|the operator'?s own account|self[- ]authored|posted by the operator)"
)
HUMAN_WORDS = (
    "(?:human|person|people|collaborator|teammate|colleague|engineer|contributor|"
    "co-?founder|candidate|real person)"
)
NEGATION = (
    "(?:not|no|never|non|neither|nor|isn'?t|aren'?t|wasn'?t|rather than|instead of|"
    "as opposed to)"
)

_KNOWN_KINDS = ("human", "automation", "operator_self", "unknown")
_ACTOR_LINE = re.compile(r"actor:\s*[^\n()]+\([^\n()]+\)\s*", _FLAGS)

_UUID_ID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", _FLAGS
)
_COMMENT_ID_FIELD = re.compile(
    r"\bcomment[_ ]?id\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9_-]*)", _FLAGS
)
_COMMENT_WORD_ID = re.compile(
    r"\bcomment(?:\s+id)?\s*[:#]?\s*[\"']?([A-Za-z0-9][A-Za-z0-9_-]{3,})", _FLAGS
)

TRUNCATING_FINISH_REASONS = ("length", "max_tokens", "max_output_tokens", "token_limit")

_FLOOR_FAILURE = re.compile(
    r"\bproven floor\b|\bexit(?:ed|s)?(?:\s+code)?\s*[:=]?\s*3\b", _FLAGS
)
_TIMEOUT_FAILURE = re.compile(
    r"ran about \d+\s*minutes? against a \d+\s*minute shell budget|"
    r"\b(?:shell|delegation|execution) budget\b|\btimed?[ -]?out\b|"
    r"\bpeer reconcile holds\b|\bbackground time limit\b",
    _FLAGS,
)
_TRUNCATED_FAILURE = re.compile(r"cut off mid-sentence|\btruncat", _FLAGS)
_UNSOURCED_FAILURE = re.compile(
    r"facts? table (?:does|do) ?n[o']t support|not supported by the facts|"
    r"facts? (?:does|do) ?n[o']t (?:support|answer|contain)|\bunsupported\b|"
    r"\binvented\b|\bfabricat|not in the facts",
    _FLAGS,
)


def _js_json(value: JsonType | Sequence[str]) -> str:
    """JSON.stringify: no spaces, non-ASCII kept."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def strip_negated_clauses(text: str, words: str) -> str:
    """Remove "<negation> ... <marker>" within one clause so a denial is not a marker."""
    pattern = re.compile(
        "\\b" + NEGATION + "\\b[^,;.()\\[\\]]{0,48}?\\b" + words + "\\b", _FLAGS
    )
    return pattern.sub(" ", text)


def has_marker(text: str, words: str) -> bool:
    return re.search("\\b" + words + "\\b", text, _FLAGS) is not None


def classify_author_role(role: str) -> str:
    """Free-text author_role -> human | automation | operator_self | unknown."""
    raw = role.strip()
    if not raw:
        return "unknown"
    residual = strip_negated_clauses(
        strip_negated_clauses(raw, AUTOMATION_WORDS), SELF_WORDS
    )
    if has_marker(residual, AUTOMATION_WORDS):
        return "automation"
    if re.search(SELF_WORDS, residual, _FLAGS):
        return "operator_self"
    if has_marker(residual, HUMAN_WORDS):
        return "human"
    return "unknown"


def has_actor_line(text: str) -> bool:
    first = text.split("\n", 1)[0].strip()
    return _ACTOR_LINE.fullmatch(first) is not None


def classify_author(item: ModelCollectedItem) -> str:
    """An actor line beats the collector's judgement; operator_self folds into human."""
    if has_actor_line(item.text):
        return "automation"
    declared = item.author_kind.strip().lower()
    kind = (
        declared
        if declared in _KNOWN_KINDS and declared != "unknown"
        else classify_author_role(item.author_role)
    )
    return "human" if kind == "operator_self" else kind


def decide_item(item: ModelCollectedItem) -> ModelItemDecision:
    """THE decision for one comment; it emits its own reason."""
    kind = EnumAuthorKind(classify_author(item))
    role = item.author_role or "(none recorded)"

    def decision(draft: bool, reason_class: str, reason: str) -> ModelItemDecision:
        return ModelItemDecision(
            ticket=item.ticket,
            comment_id=item.comment_id,
            comment_url=item.comment_url,
            author_role=role,
            author_kind=kind,
            draft=draft,
            reason_class=reason_class,
            reason=reason,
        )

    if not item.text.strip():
        return decision(
            False, "empty_text", "the collector recorded no comment text for this item"
        )
    if kind is EnumAuthorKind.AUTOMATION:
        reason = (
            f"agent-posted: {item.text.split(chr(10), 1)[0].strip()}"
            if has_actor_line(item.text)
            else f'automation-authored: the collector\'s author_role reads "{role}"'
        )
        return decision(False, "automation", reason)
    if item.already_answered_by.strip():
        return decision(
            False,
            "already_answered",
            f"already answered in-thread by {item.already_answered_by}",
        )
    if not item.needs_reply:
        return decision(
            False,
            "no_reply_needed",
            "the collector set needs_reply false for this comment",
        )
    if kind is EnumAuthorKind.UNKNOWN:
        return decision(
            False,
            "author_unclassified",
            f'HELD for operator adjudication: author_role "{role}" names neither a human '
            "collaborator nor an automation, so this item was not classified either way",
        )
    return decision(True, "", "")


def source_comment_ids(item: ModelCollectedItem) -> tuple[str, ...]:
    cid = item.comment_id.strip()
    return (cid,) if cid else ()


def build_prompt(item: ModelCollectedItem, facts: str) -> str:
    """The prompt handed to the delegated drafter: the item and the facts table, nothing else."""
    asks = list(item.asks)
    asks_line = (
        f"Asks: {_js_json(asks)}."
        if asks
        else "Asks: the collector did not enumerate them. Read the comment above and answer "
        "what it actually asks. If it asks nothing answerable, say so in "
        "open_points_for_operator and leave proposed_reply empty rather than writing a "
        "filler acknowledgement."
    )
    return "\n".join(
        [
            f"Draft the operator's reply comment for Linear {item.ticket} "
            f"({item.ticket_title}), replying to comment {item.comment_id} by "
            f"{item.author_role} at {item.posted_at}.",
            "",
            "SOURCE SET -- the only comment ids and comment texts you may cite. Cite no "
            "comment id outside this set; every comment_id in your answer is the one below.",
            f"comment id: {', '.join(source_comment_ids(item))}",
            "comment text:",
            "---",
            item.text,
            "---",
            asks_line,
            "",
            "Ground-truth facts you may cite, and nothing else:",
            facts,
            "",
            "Operator voice: plain, direct, first person, SHORT (3 to 10 sentences), no "
            "bullets unless listing options, no em-dashes. Answer every ask with a verified "
            "fact and cite the ticket, PR, run id or ledger line it comes from. Never invent "
            "a fact, never use a placeholder, and never promise a date the facts do not "
            "support. Where only the operator can decide, write a bracketed [OPERATOR: ...] "
            "line naming the options.",
            "",
            "Never write a holding non-answer: no 'I'll look into it', no 'we're tracking "
            "this', no 'more soon', no bare acknowledgement of receipt. If the facts above "
            "do not answer an ask, that ask goes in open_points_for_operator and does not "
            "appear in proposed_reply at all. A reply that answers nothing must come back "
            "with proposed_reply empty.",
            "",
            "Do not echo the asker's own uncertainty phrasing back at them, and never write "
            "a disclaimer about what you or they do not know. State what is true. When "
            "something the asker wants is genuinely not in the facts above, do not hedge "
            "about it in the reply text: leave it out of proposed_reply and put it in "
            "open_points_for_operator instead.",
            "",
            "Your entire output must be one JSON object and nothing else. The first "
            "character you emit is { and the last is }. No reasoning, no preamble, no "
            "explanation, no code fence, no text after the object.",
            "Keys: ticket, comment_id, their_summary, proposed_reply, facts_cited (array of "
            "strings), open_points_for_operator (array of strings), confidence (one of "
            "high, medium, low).",
            "In their_summary, paraphrase what they asked in your own words. Never quote "
            "their comment verbatim anywhere in the object.",
        ]
    )


def _looks_like_an_id(token: str) -> bool:
    if not re.search(r"\d", token):
        return False
    if re.match(r"\d{4}-\d{2}-\d{2}", token):
        return False
    return re.fullmatch(r"[A-Z]{2,}-\d+", token) is None


def extract_comment_ids(text: str) -> list[str]:
    """Every comment-id-shaped token, in order of appearance, first spelling kept."""
    found: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            found.append(token)

    for match in _UUID_ID.finditer(text):
        add(match.group(0))
    for match in _COMMENT_ID_FIELD.finditer(text):
        add(match.group(1))
    for match in _COMMENT_WORD_ID.finditer(text):
        if _looks_like_an_id(match.group(1)):
            add(match.group(1))
    return found


def check_draft_cites_only_source_ids(
    draft: ModelReplyDraft, item: ModelCollectedItem, facts: str
) -> ModelDraftCitationCheck:
    """Refuse a draft citing an id outside the source set and facts, or answering another comment."""
    ids = source_comment_ids(item)
    allowed = {x.lower() for x in ids}
    haystack = (facts + "\n" + item.text).lower()
    strings = [draft.their_summary, draft.proposed_reply, *draft.facts_cited]
    strings += list(draft.open_points_for_operator)
    foreign = [
        found
        for found in extract_comment_ids("\n".join(strings))
        if found.lower() not in allowed and found.lower() not in haystack
    ]
    wanted = ids[0] if ids else ""
    mismatch = (
        draft.comment_id if draft.comment_id is not None else ""
    ).strip() != wanted
    parts: list[str] = []
    if foreign:
        parts.append(
            f"the draft cites comment id(s) {', '.join(foreign)} that are in neither the "
            "source set nor the facts table"
        )
    if mismatch:
        parts.append(
            f"the draft's comment_id {_js_json(draft.comment_id)} is not the item's "
            f"{_js_json(wanted)}"
        )
    return ModelDraftCitationCheck(
        ok=not parts,
        reason="; ".join(parts),
        foreign_ids=tuple(foreign),
        comment_id_mismatch=mismatch,
    )


def _strip_fence_and_space(text: str) -> str:
    text = re.sub(r"\s+\Z", "", text)
    text = re.sub(r"```\s*\Z", "", text)
    return re.sub(r"\s+\Z", "", text)


def detect_truncation(outcome: ModelDelegateOutcome) -> ModelTruncationCheck:
    """From the receipt (truncated, finish_reason) plus an unclosed-JSON structural check."""
    signals: list[str] = []
    if outcome.truncated is True:
        signals.append("receipt_truncated")
    reason = (outcome.finish_reason or "").strip().lower()
    if reason in TRUNCATING_FINISH_REASONS:
        signals.append(f"finish_reason:{reason}")
    head = re.sub(
        r"\A\s*(?:```[a-z]*\s*)?", "", outcome.response_head or "", flags=_FLAGS
    )
    tail = _strip_fence_and_space(outcome.response_tail or "")
    if head[:1] == "{" and tail and tail[-1] != "}":
        signals.append("unclosed_json")
    return ModelTruncationCheck(truncated=bool(signals), signals=tuple(signals))


def classify_held(held: ModelHeldInput) -> EnumHeldClass:
    """Total: whatever arrives, one member of the closed set leaves."""
    if _FLOOR_FAILURE.search(held.reason):
        return EnumHeldClass.FLOOR_REFUSED
    if _TIMEOUT_FAILURE.search(held.reason):
        return EnumHeldClass.TIMEOUT
    if held.truncated or _TRUNCATED_FAILURE.search(held.reason):
        return EnumHeldClass.TRUNCATED
    if held.stage == "source_check" or _UNSOURCED_FAILURE.search(held.reason):
        return EnumHeldClass.UNSOURCED
    if held.stage == "verify" and held.reason.strip():
        return EnumHeldClass.UNSOURCED
    return EnumHeldClass.TRANSPORT


def held_by_class(held: Sequence[ModelHeldInput]) -> dict[str, int]:
    counts = {member.value: 0 for member in EnumHeldClass}
    for entry in held:
        declared = entry.reason_class
        cls = declared if declared in counts else classify_held(entry).value
        counts[cls] += 1
    return counts


def is_degraded(needing_count: int, accepted_count: int) -> bool:
    """A window that had something to answer and accepted nothing."""
    return needing_count > 0 and accepted_count == 0


def _or_none(*values: JsonType) -> JsonType:
    """JavaScript ``a || b || null``: the first truthy value, else null."""
    for value in values:
        if value:
            return value
    return None


def _dict(value: JsonType) -> dict[str, JsonType]:
    return value if isinstance(value, dict) else {}


def metrics_row(receipt: dict[str, JsonType]) -> ModelMetricsRow:
    """Which model answered. ``model_cloud_baseline`` is the premium counterfactual, never the answerer."""
    inner = _dict(receipt.get("receipt"))
    result = _dict(inner.get("result"))
    metrics = _dict(result.get("metrics"))
    attempts = result.get("attempts")
    tried = tuple(
        ModelAttemptRow(
            tier=_dict(a).get("tier"),
            backend_id=_dict(a).get("backend_id"),
            model_id=_dict(a).get("model_id"),
            quality_score=_dict(a).get("quality_score"),
            decision=_dict(a).get("acceptance_decision"),
            cost_usd=_dict(a).get("cost_usd"),
        )
        for a in (attempts if isinstance(attempts, list) else [])
    )
    last = tried[-1] if tried else ModelAttemptRow()
    attempts_count = result.get("attempts_count")
    return ModelMetricsRow(
        model=_or_none(result.get("model_name"), last.model_id),
        backend_id=_or_none(receipt.get("backend_id"), last.backend_id),
        tier=last.tier or None,
        total_tokens=metrics.get("total_tokens"),
        input_tokens=metrics.get("input_tokens"),
        output_tokens=metrics.get("output_tokens"),
        latency_ms=metrics.get("latency_ms"),
        wall_ms=inner.get("duration_ms"),
        cost_usd=metrics.get("cost_usd"),
        cost_savings_usd=metrics.get("cost_savings_usd"),
        premium_counterfactual_model=result.get("model_cloud_baseline") or None,
        attempts=len(tried) if attempts_count is None else attempts_count,
        escalations=result.get("escalation_count"),
        accepted=last.decision == "accept",
        tried=tried,
    )


class HandlerLinearCommentsDecision:
    """Stateless compute: one request kind in, one typed answer out."""

    def handle(
        self, request: ModelLinearCommentsDecisionRequest
    ) -> ModelLinearCommentsDecisionResult:
        kind = request.kind
        if (
            kind is EnumLinearCommentsDecisionKind.DECIDE_ITEMS
            and request.items is not None
        ):
            decisions = tuple(decide_item(item) for item in request.items)
            return ModelLinearCommentsDecisionResult(
                kind=kind,
                decisions=decisions,
                selected_comment_ids=tuple(d.comment_id for d in decisions if d.draft),
            )
        if (
            kind is EnumLinearCommentsDecisionKind.BUILD_PROMPT
            and request.item is not None
        ):
            return ModelLinearCommentsDecisionResult(
                kind=kind, prompt=build_prompt(request.item, request.facts or "")
            )
        if (
            kind is EnumLinearCommentsDecisionKind.CHECK_DRAFT
            and request.draft is not None
            and request.item is not None
        ):
            return ModelLinearCommentsDecisionResult(
                kind=kind,
                citation=check_draft_cites_only_source_ids(
                    request.draft, request.item, request.facts or ""
                ),
            )
        if (
            kind is EnumLinearCommentsDecisionKind.DETECT_TRUNCATION
            and request.outcome is not None
        ):
            return ModelLinearCommentsDecisionResult(
                kind=kind, truncation=detect_truncation(request.outcome)
            )
        if (
            kind is EnumLinearCommentsDecisionKind.CLASSIFY_HELD
            and request.held is not None
        ):
            return ModelLinearCommentsDecisionResult(
                kind=kind, held_classes=tuple(classify_held(h) for h in request.held)
            )
        if (
            kind is EnumLinearCommentsDecisionKind.TALLY
            and request.held is not None
            and request.needing_count is not None
            and request.accepted_count is not None
        ):
            return ModelLinearCommentsDecisionResult(
                kind=kind,
                held_by_class=held_by_class(request.held),
                degraded=is_degraded(request.needing_count, request.accepted_count),
            )
        if (
            kind is EnumLinearCommentsDecisionKind.METRICS_ROW
            and request.receipt is not None
        ):
            return ModelLinearCommentsDecisionResult(
                kind=kind, metrics=metrics_row(request.receipt)
            )
        raise ValueError(f"{kind.value}: request is missing its inputs")
