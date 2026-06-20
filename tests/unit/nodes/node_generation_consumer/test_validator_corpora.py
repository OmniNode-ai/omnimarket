# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural tests for the G2 validator acceptance corpora (OMN-13294).

These assert each registered corpus is RUNNABLE by the corpus-acceptance gate:
non-empty, carries at least one adversarial mutation case (an all-base-case corpus
is rejected by ``evaluate_corpus_acceptance``), and that the canonical
hand-authored scanner for the invariant passes it. This guarantees the corpus the
generation run is gated against is itself well-formed before any model is called.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_generation_consumer.corpus_acceptance import (
    evaluate_corpus_acceptance,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora import CORPORA

# A correct hand-authored RFC1918 scanner — the reference the corpus must accept.
# Octet-parsing so version strings / public IPs are excluded (the boundary cases).
# Uses only explicit comparisons (no `any` builtin — the hardened acceptance
# sandbox denies several builtins, exactly the constraint a real local model also
# generates around; see the G1 dogfood finding in the local_paths provenance).
_REFERENCE_IP_SCANNER = """\
import re

def handle(input_data):
    source = input_data.get("source", "")
    pat = re.compile(r"\\b(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\b")
    findings = []
    for line in source.split("\\n"):
        if "onex-allow-internal-ip" in line:
            continue
        for m in pat.finditer(line):
            a = int(m.group(1))
            b = int(m.group(2))
            c = int(m.group(3))
            d = int(m.group(4))
            if a > 255 or b > 255 or c > 255 or d > 255:
                continue
            if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
                findings.append({"ip": m.group()})
    return {"findings": findings}
"""


@pytest.mark.unit
def test_registry_is_non_empty() -> None:
    assert CORPORA, "the G2 corpus registry must declare at least one corpus"


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(CORPORA))
def test_each_corpus_is_runnable(name: str) -> None:
    corpus = CORPORA[name]
    assert not corpus.is_empty, f"{name}: corpus must carry fixtures"
    assert corpus.has_mutation_case, (
        f"{name}: corpus must carry at least one adversarial mutation case "
        "(an all-base-case corpus is rejected by the acceptance gate)"
    )
    assert corpus.violation_fixtures, f"{name}: needs violation fixtures"
    assert corpus.clean_fixtures, f"{name}: needs clean fixtures"


@pytest.mark.unit
def test_hardcoded_ip_corpus_accepts_the_reference_scanner() -> None:
    # The corpus must be satisfiable by a correct scanner — a corpus no correct
    # implementation can pass is mis-specified.
    result = evaluate_corpus_acceptance(
        _REFERENCE_IP_SCANNER, CORPORA["hardcoded-private-ip"]
    )
    assert result.checked is True
    assert result.passed is True, f"reference scanner failed corpus: {result.errors}"
    assert result.errors == []


@pytest.mark.unit
def test_hardcoded_ip_corpus_rejects_a_false_negative_scanner() -> None:
    # A scanner that only flags the .201 server misses every other private IP —
    # exactly the silent-false-negative the corpus exists to catch.
    false_negative = (
        "def handle(input_data):\n"
        "    s = input_data.get('source', '')\n"
        "    hits = ['x'] if '192.168.86.201' in s else []\n"
        "    return {'findings': hits}\n"
    )
    result = evaluate_corpus_acceptance(false_negative, CORPORA["hardcoded-private-ip"])
    assert result.checked is True
    assert result.passed is False
    assert result.errors
