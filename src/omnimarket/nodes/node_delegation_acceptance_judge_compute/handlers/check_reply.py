# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate one judge reply against the item ids it had to answer. Pure."""

from __future__ import annotations

import json
import re
from collections import Counter

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_issue_code import (
    EnumAcceptanceIssueCode as Code,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_issue import (
    ModelAcceptanceIssue,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_rubric import (
    ModelAcceptanceRubric,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)

_FENCE = re.compile(r"\A```[A-Za-z0-9_-]*[ \t]*\n(.*)\n```\s*\Z", re.DOTALL)


def _unwrap(reply: str) -> str:
    """The reply is JSON alone or JSON in one code fence; nothing else is tolerated."""
    text = reply.strip()
    fenced = _FENCE.match(text)
    return fenced.group(1).strip() if fenced else text


def _entries(root: object) -> list[object] | None:
    if isinstance(root, list):
        return root
    if isinstance(root, dict):
        judgments = root.get("judgments")
        if isinstance(judgments, list):
            return judgments
    return None


def _entry_issues(
    entry: dict[str, object], item_id: str, rubric: ModelAcceptanceRubric
) -> list[ModelAcceptanceIssue]:
    issues: list[ModelAcceptanceIssue] = []

    def add(code: Code, message: str) -> None:
        issues.append(ModelAcceptanceIssue(code=code, item_id=item_id, message=message))

    accept = entry.get("accept")
    quality = entry.get("quality")
    failure_class = entry.get("failure_class")
    reason = entry.get("reason")
    if not isinstance(accept, bool):
        add(Code.BAD_SHAPE, "accept must be true or false")
    quality_ok = (
        isinstance(quality, int) and not isinstance(quality, bool) and 0 <= quality <= 3
    )
    if not quality_ok:
        add(Code.BAD_QUALITY, "quality must be an integer from 0 to 3")
    valid_class = isinstance(failure_class, str) and failure_class in {
        member.value for member in EnumAcceptanceFailureClass
    }
    if not valid_class:
        add(
            Code.BAD_FAILURE_CLASS,
            f"failure_class {failure_class!r} is not in the vocabulary",
        )
    if not isinstance(reason, str) or not reason.strip():
        add(Code.BAD_REASON, "reason must be a non-empty string")
    elif len(reason) > rubric.thresholds.reason_max_chars:
        add(
            Code.REASON_TOO_LONG,
            f"reason is {len(reason)} characters, the limit is {rubric.thresholds.reason_max_chars}",
        )
    if isinstance(accept, bool) and quality_ok and valid_class:
        none = failure_class == EnumAcceptanceFailureClass.NONE.value
        if (
            accept
            and isinstance(quality, int)
            and quality < rubric.thresholds.accept_min_quality
        ):
            add(
                Code.ACCEPT_QUALITY_TOO_LOW,
                f"an accept needs quality of at least {rubric.thresholds.accept_min_quality}",
            )
        if accept and not none:
            add(
                Code.ACCEPT_WITH_FAILURE_CLASS,
                "an accept must carry failure_class none",
            )
        if not accept and none:
            add(Code.REJECT_WITHOUT_FAILURE_CLASS, "a reject must name a failure class")
    return issues


def check_reply(
    reply_text: str, expected_ids: tuple[str, ...], rubric: ModelAcceptanceRubric
) -> tuple[list[ModelAcceptanceVerdict], list[ModelAcceptanceIssue]]:
    """Return the verdicts that are sound and every issue found; an item with an issue has no verdict."""
    try:
        root = json.loads(_unwrap(reply_text))
    except json.JSONDecodeError as exc:
        return [], [
            ModelAcceptanceIssue(
                code=Code.UNPARSEABLE_REPLY,
                message=f"the reply is not JSON alone or JSON in one code fence: {exc.msg}",
            )
        ]
    entries = _entries(root)
    if entries is None:
        return [], [
            ModelAcceptanceIssue(
                code=Code.BAD_SHAPE,
                message='the reply must be {"judgments": [...]} or a list of judgments',
            )
        ]
    expected = set(expected_ids)
    issues: list[ModelAcceptanceIssue] = []
    ids = [
        entry.get("item_id") if isinstance(entry, dict) else None for entry in entries
    ]
    counts = Counter(i for i in ids if isinstance(i, str))
    by_id: dict[str, dict[str, object]] = {}
    reported_duplicates: set[str] = set()
    for entry, item_id in zip(entries, ids, strict=True):
        if not isinstance(entry, dict) or not isinstance(item_id, str) or not item_id:
            issues.append(
                ModelAcceptanceIssue(
                    code=Code.BAD_SHAPE, message="a judgment lacks a string item_id"
                )
            )
        elif item_id not in expected:
            issues.append(
                ModelAcceptanceIssue(
                    code=Code.UNKNOWN_ITEM,
                    item_id=item_id,
                    message="the batch has no such item",
                )
            )
        elif counts[item_id] > 1:
            if item_id not in reported_duplicates:
                reported_duplicates.add(item_id)
                issues.append(
                    ModelAcceptanceIssue(
                        code=Code.DUPLICATE_ITEM,
                        item_id=item_id,
                        message=f"answered {counts[item_id]} times",
                    )
                )
        else:
            by_id[item_id] = entry
    verdicts: list[ModelAcceptanceVerdict] = []
    for item_id in expected_ids:
        if item_id in by_id:
            entry_issues = _entry_issues(by_id[item_id], item_id, rubric)
            issues.extend(entry_issues)
            if not entry_issues:
                entry = by_id[item_id]
                verdicts.append(
                    ModelAcceptanceVerdict(
                        item_id=item_id,
                        accept=bool(entry["accept"]),
                        quality=int(str(entry["quality"])),
                        failure_class=EnumAcceptanceFailureClass(
                            str(entry["failure_class"])
                        ),
                        reason=str(entry["reason"]).strip(),
                    )
                )
        elif counts[item_id] == 0:
            issues.append(
                ModelAcceptanceIssue(
                    code=Code.MISSING_ITEM,
                    item_id=item_id,
                    message="the reply has no judgment",
                )
            )
    return verdicts, issues
