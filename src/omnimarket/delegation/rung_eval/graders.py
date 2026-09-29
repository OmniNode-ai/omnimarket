# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic classification and ticket placement grading."""

import re

from omnimarket.delegation.rung_eval.models import ModelGrade


def grade_classification(response: str, answer_key: dict[str, str]) -> ModelGrade:
    answers: dict[str, str] = {}
    for line in response.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) >= 2 and re.fullmatch(r"[\w.-]+#\d+", fields[0]):
            answers[fields[0]] = fields[1]
    missing = sum(key not in answers for key in answer_key)
    wrong = sum(
        key in answers and answers[key] != expected
        for key, expected in answer_key.items()
    )
    score = (len(answer_key) - missing - wrong) / len(answer_key) if answer_key else 0.0
    return ModelGrade(
        score=score, passed=score == 1.0, detail=f"wrong={wrong}; missing={missing}"
    )


def grade_grouping(
    response: str,
    ticket_ids: list[str],
    known_ids: list[str],
    max_groups: int,
) -> ModelGrade:
    groups = [line for line in response.splitlines() if line.startswith("GROUP ")]
    park = re.search(r"(?m)^PARK:", response)
    placement_text = "\n".join(groups)
    if park is not None:
        placement_text += "\n" + response[park.end() :]
    placed = set(re.findall(r"\bOMN-\d+\b", placement_text))
    missing = sorted(set(ticket_ids) - placed)
    invented = sorted(set(re.findall(r"\bOMN-\d+\b", response)) - set(known_ids))
    score = (
        sum(ticket_id in placed for ticket_id in ticket_ids) / len(ticket_ids)
        if ticket_ids and not invented and park is not None
        else 0.0
    )
    detail = f"groups={len(groups)}/{max_groups}; PARK={park is not None}; missing={len(missing)}"
    if invented:
        detail += f"; invented ids: {', '.join(invented)}"
    return ModelGrade(
        score=score,
        passed=1 <= len(groups) <= max_groups
        and park is not None
        and not missing
        and not invented,
        detail=detail,
    )
