# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-26728 contract records eight tier receipts, their verdicts and the refusal."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26728.yaml"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TIERS = (
    "local lab rung, Qwen3.8-27B",
    "claude-glm glm-5.3-flash",
    "claude-glm glm-5.3,",
    "claude-glm glm-5.4",
    "claude claude-haiku-5-5",
    "claude claude-sonnet-5-5",
    "claude claude-opus-5-5",
    "codex codex-account-default",
)
VERDICTS = (
    "local edited",
    "GLM small edited",
    "GLM medium used",
    "GLM large edited",
    "Claude small edited",
    "Claude medium edited",
    "Claude large used",
    "Codex refused",
)


def evidence(text: str, item: str) -> str:
    data = yaml.safe_load(text)
    return " ".join(
        word
        for e in data["dod_evidence"]
        if e["id"] == item
        for word in str(e["description"]).split()
    )


def receipts_ok(text: str) -> bool:
    body = evidence(text, "dod-omn26728-eight-tier-receipts")
    return (
        all(re.search(re.escape(tier) + r".*?" + UUID, body) for tier in TIERS)
        and body.count("done") + body.count("completed") >= 7
    )


def verdicts_ok(text: str) -> bool:
    body = evidence(text, "dod-omn26728-verdicts")
    return all(v in body for v in VERDICTS)


def refusal_ok(text: str) -> bool:
    body = evidence(text, "dod-omn26728-codex-refusal")
    return "HarnessPlacementRefused" in body and "pull request 2099" in body


def test_eight_tier_receipts_name_harness_model_outcome_and_run() -> None:
    assert receipts_ok(CONTRACT.read_text())


def test_every_tier_has_a_used_edited_or_refused_verdict() -> None:
    assert verdicts_ok(CONTRACT.read_text())


def test_failed_tier_names_typed_refusal_and_fix_reference() -> None:
    assert refusal_ok(CONTRACT.read_text())


def test_known_bad_contract_is_refused() -> None:
    text = CONTRACT.read_text()
    assert not receipts_ok(text.replace("claude claude-opus-5-5", "claude-opus"))
    assert not verdicts_ok(text.replace("Claude large used", "Claude large"))
    assert not refusal_ok(text.replace("HarnessPlacementRefused", "refused"))
