# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A backend id must not name a model family its declared model is not (OMN-19455).

A backend id is what receipts, projections and operators read. When it names a
model the backend does not serve (``openrouter-qwen3-coder-480b`` serving a
nemotron slug, ``cloud-gemini-pro`` serving ``gemini-2.5-flash``) every reader
is told the wrong thing. The fact table (backend id, declared ``model_name``)
is built from the delegation contract; the id may not carry a family token, or
the ``pro`` variant token, that the declared model lacks. Backends with no
declared ``model_name`` (local rungs, served id from the lane overlay) are
unmeasured and never fail.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, NamedTuple

import yaml

_BIFROST_CONTRACT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

#: Model family tokens, matched as whole tokens of the id and of the model name.
FAMILY_TOKENS: Final[frozenset[str]] = frozenset(
    {"gemini", "glm", "qwen", "qwen3", "nemotron", "llama", "jev", "cohere", "north"}
)
#: Variant tokens that, when named by the id, the model must name too.
VARIANT_TOKENS: Final[frozenset[str]] = frozenset({"pro"})


class BackendFact(NamedTuple):
    backend_id: str
    model_name: str | None


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def build_fact_table(path: Path = _BIFROST_CONTRACT_PATH) -> list[BackendFact]:
    """Every backend_id in the contract with its declared model_name."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        BackendFact(b["backend_id"], b.get("model_name"))
        for b in data.get("backends", [])
    ]


def find_misdescriptions(facts: list[BackendFact]) -> list[str]:
    """One message per backend whose id names a family/variant its model lacks."""
    problems: list[str] = []
    for fact in facts:
        if not fact.model_name:
            continue
        id_tokens = _tokens(fact.backend_id)
        model_tokens = _tokens(fact.model_name)
        for token in sorted((FAMILY_TOKENS | VARIANT_TOKENS) & id_tokens):
            if token not in model_tokens:
                problems.append(
                    f"backend id {fact.backend_id!r} names {token!r} but declares "
                    f"model {fact.model_name!r}"
                )
    return problems


def test_committed_contract_has_no_misdescribing_backend_id() -> None:
    facts = build_fact_table()
    assert len(facts) > 5  # positive control: the table is not empty
    assert find_misdescriptions(facts) == []


def test_ticket_examples_are_caught() -> None:
    facts = [
        BackendFact(
            "openrouter-qwen3-coder-480b", "nvidia/nemotron-3-ultra-550b-a55b:free"
        ),
        BackendFact("cloud-gemini-pro", "gemini-2.5-flash"),
        BackendFact("cloud-glm-judge", "gemini-2.5-flash"),
    ]
    problems = find_misdescriptions(facts)
    assert len(problems) == 3
    assert any("qwen3" in p for p in problems)
    assert any("'pro'" in p for p in problems)
    assert any("'glm'" in p for p in problems)


def test_truthful_ids_and_unmeasured_local_backends_pass() -> None:
    facts = [
        BackendFact(
            "openrouter-nemotron-ultra", "nvidia/nemotron-3-ultra-550b-a55b:free"
        ),
        BackendFact("cloud-gemini-2-5-flash", "gemini-2.5-flash"),
        BackendFact("cloud-glm-5-3", "glm-5.3"),
        BackendFact("local-coder", None),
    ]
    assert find_misdescriptions(facts) == []


def test_renamed_ids_are_the_ids_in_the_contract() -> None:
    ids = {f.backend_id for f in build_fact_table()}
    assert {
        "openrouter-nemotron-ultra",
        "cloud-gemini-2-5-flash",
        "cloud-gemini-judge",
    } <= ids
    assert not ids & {
        "openrouter-qwen3-coder-480b",
        "cloud-gemini-pro",
        "cloud-glm-judge",
    }
