# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decision of one hook refusal's ledger row.

Ported without a behaviour change from the omniclaude hook refusal recorder
(redact, normalise_reason, dedupe_key, build_row and the recorder's field
handling): one FRICTION row per refusal, keyed so a guard in a retry loop
collapses to one row per class, lane and window. The rate-limit state, the
ledger append and the lane resolution stay with the caller; this handler reads
no clock, file or environment.
"""

import hashlib
import re

from omnimarket.nodes.node_hook_refusal_row_compute.models import (
    ModelHookRefusalRowRequest,
    ModelHookRefusalRowResult,
)

ROW_CLASS = "FRICTION"
MAX_DETAIL_CHARS = 240
MAX_GUARD_CHARS = 64
MAX_REASON_CHARS = 64
MAX_REASON_SEGMENTS = 8
SECRET_REPEAT_GUARD = "subagent_stop_secret_leak_guard.sh"

# A refusal message quotes the refused command, and a refused command is
# exactly the kind that carries a token: the redaction is deliberately blunt.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(secret|token|password|api[_-]?key)\s*[=:]\s*\S+"),
)
# A path, URL or branch name is the instance, not the class: dropped whole,
# before slugification, so one path never survives as word-shaped segments.
_PATHLIKE_SEGMENT = re.compile(r"\S*[/\\]\S*")
_BARE_NUMBER = re.compile(r"^[0-9]+$")
_SLUG_SPLIT = re.compile(r"[^a-z0-9]+")
# The ledger is pipe-delimited and line-oriented: a pipe or newline inside a
# field would forge a column or a row.
_FIELD_BREAKERS = re.compile(r"[|\r\n]+")


def redact(text: str) -> str:
    """Strip credential-shaped substrings and anything that breaks a row."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return _FIELD_BREAKERS.sub(" ", text).strip()


def normalise_reason(reason: str) -> str:
    """Collapse a guard's reason text into a stable, low-cardinality token."""
    lowered = _PATHLIKE_SEGMENT.sub(" ", reason.strip().lower())
    kept = [
        seg for seg in _SLUG_SPLIT.split(lowered) if seg and not _BARE_NUMBER.match(seg)
    ]
    slug = "-".join(kept[:MAX_REASON_SEGMENTS])
    return slug[:MAX_REASON_CHARS] or "unspecified"


def dedupe_key(guard: str, reason: str, lane: str) -> str:
    """Stable short key for one refusal class, excluding detail and command."""
    raw = f"{guard}\x1f{reason}\x1f{lane}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


class HandlerHookRefusalRowCompute:
    def handle(self, request: ModelHookRefusalRowRequest) -> ModelHookRefusalRowResult:
        guard = redact(request.guard)[:MAX_GUARD_CHARS] or "unknown-guard"
        reason = normalise_reason(redact(request.reason))
        detail = redact(request.detail)[:MAX_DETAIL_CHARS]
        repeated_secret = guard == SECRET_REPEAT_GUARD
        # The secret guard's retry budget is per session, never shared by two
        # sessions that both left their lane unresolved.
        scope = (
            f"{request.lane}:{request.session}"
            if repeated_secret and request.session
            else request.lane
        )
        key = dedupe_key(guard, reason, scope)
        # Redacted again at row build: a caller passing raw text must not be
        # able to forge a column or a second row, and truncation can leave a
        # trailing space the first pass never saw.
        row_detail = redact(detail)[:MAX_DETAIL_CHARS]
        row = (
            f"{request.timestamp} | {ROW_CLASS} | "
            f"lane={redact(request.lane) or 'unresolved'} | "
            f"actor=hook | model=none | class=refusal | guard={guard} | "
            f"reason={reason} | lane_source={request.lane_source} | dedupe={key} | "
            "refusal_count=1 | "
            f"suppressed_since_last_row={request.suppressed} | "
            f'detail="{row_detail}" | existing=OMN-18946 | cost=~1 lane-minute | '
            "This row exists because a hook refusal is otherwise terminal-only "
            "and unaggregated (OMN-18946)"
        )
        return ModelHookRefusalRowResult(
            guard=guard,
            reason=reason,
            detail=detail,
            key=key,
            repeated_secret=repeated_secret,
            row=row,
        )
