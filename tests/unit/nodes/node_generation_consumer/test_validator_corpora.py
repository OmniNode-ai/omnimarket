# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-13294 reason="test exercises the corpus whose subject is hardcoded private-IP literals; the .201 literal is the false-negative probe input"
# onex-allow-internal-ip OMN-13294 reason="the .201 literal is the deliberate false-negative probe input for the corpus-acceptance test"
# test-literal-ok: OMN-13294 — the hardcoded IP is the intentional probe input the corpus-acceptance test feeds the scanner
"""Structural tests for the G2 validator acceptance corpora (OMN-13294).

These assert each registered corpus is RUNNABLE by the corpus-acceptance gate:
non-empty, carries at least one adversarial mutation case (an all-base-case corpus
is rejected by ``evaluate_corpus_acceptance``), and that the canonical
hand-authored scanner for the invariant passes it. This guarantees the corpus the
generation run is gated against is itself well-formed before any model is called.
"""

from __future__ import annotations

import inspect
import textwrap

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


# A scanner that flags NOTHING — the most dangerous gate failure (every real
# violation passes silently). Every well-specified corpus MUST reject it.
_FLAG_NOTHING_SCANNER = "def handle(input_data):\n    return {'findings': []}\n"

# A scanner that flags EVERYTHING — false-positives on every clean fixture.
# Every well-specified corpus MUST reject it too.
_FLAG_EVERYTHING_SCANNER = (
    "def handle(input_data):\n    return {'findings': [{'line': 1}]}\n"
)


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(CORPORA))
def test_corpus_rejects_a_flag_nothing_scanner(name: str) -> None:
    # Negative control (false-negative direction): a permissive gate that never
    # flags must miss every violation_fixture and so be rejected. This is what
    # makes corpus acceptance meaningful rather than always-true.
    result = evaluate_corpus_acceptance(_FLAG_NOTHING_SCANNER, CORPORA[name])
    assert result.checked is True, name
    assert result.passed is False, name
    # every violation fixture must be unflagged by the no-op scanner
    assert result.violation_flagged == 0, name
    assert result.errors, name


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(CORPORA))
def test_corpus_rejects_a_flag_everything_scanner(name: str) -> None:
    # Negative control (false-positive direction): an over-eager gate that flags
    # every input must false-flag every clean_fixture and so be rejected.
    result = evaluate_corpus_acceptance(_FLAG_EVERYTHING_SCANNER, CORPORA[name])
    assert result.checked is True, name
    assert result.passed is False, name
    # every clean fixture must be false-flagged by the flag-all scanner
    assert result.clean_passed == 0, name
    assert result.errors, name


# Correct hand-authored reference scanners for the G2 long-tail invariants — the
# implementations the corpus must ACCEPT. A corpus no correct scanner can pass is
# mis-specified. Each uses only the stdlib `re` and explicit logic (the hardened
# acceptance sandbox denies several builtins, the same constraint a real local
# model generates around). They are defined as real module-level functions and
# fed to the sandbox via inspect.getsource so there is no source-in-source escaping.


def _reference_localhost_url_handle(input_data):
    import re

    source = input_data.get("source", "")
    pat = re.compile(r"""https?://(localhost|127\.0\.0\.1)(?=[:/"']|$)""")
    findings = []
    for line in source.split("\n"):
        if "onex-allow-internal-ip" in line:
            continue
        if pat.search(line):
            findings.append({"url": line})
    return {"findings": findings}


def _reference_topic_handle(input_data):
    import re

    source = input_data.get("source", "")
    pat = re.compile(r"""['"]onex(\.[a-z][a-z0-9_]*){3,}['"]""")
    findings = []
    for line in source.split("\n"):
        if pat.search(line):
            findings.append({"topic": line})
    return {"findings": findings}


def _reference_todo_handle(input_data):
    import re

    source = input_data.get("source", "")
    pat = re.compile(r"\b(TODO|FIXME|HACK)\b")
    findings = []
    for line in source.split("\n"):
        if pat.search(line):
            findings.append({"marker": line})
    return {"findings": findings}


def _reference_doc_content_handle(input_data):
    import re

    source = input_data.get("source", "")
    lines = source.split("\n")
    # Whole-file suppression: a `doc-content-file-ok` marker anywhere silences
    # every leak in the file (historical-trace escape hatch).
    for whole in lines:
        if "doc-content-file-ok" in whole:
            return {"findings": []}

    # RFC1918 private bands (10/8, 172.16-31/12, 192.168/16). Doc-reserved ranges
    # (192.0.2, 198.51.100, 203.0.113), loopback (127.0.0.1) and public IPs are
    # NOT private and stay clean by octet parsing.
    ip_pat = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
    # `.201`/`.200` host shorthand NOT preceded by a digit (so `0.200`, `2.201.0`
    # decimals/SemVer do not fire).
    host_pat = re.compile(r"(?<!\d)\.20[01]\b")
    # Personal home paths; `$OMNI_HOME` / `Path.home()` portable forms do not match.
    path_pat = re.compile(r"/(Users|home)/[A-Za-z0-9_]")
    # Email leak; the example.com reserved doc domain is excluded.
    email_pat = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    omn_pat = re.compile(r"\bOMN-\d+\b")

    findings = []
    for line in lines:
        # Per-line suppression escape hatch.
        if "doc-content-ok" in line:
            continue
        flagged = False
        for m in ip_pat.finditer(line):
            a = int(m.group(1))
            b = int(m.group(2))
            if a > 255 or b > 255 or int(m.group(3)) > 255 or int(m.group(4)) > 255:
                continue
            if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
                flagged = True
        if host_pat.search(line):
            flagged = True
        if path_pat.search(line):
            flagged = True
        for em in email_pat.finditer(line):
            if not em.group().endswith("example.com"):
                flagged = True
        if omn_pat.search(line):
            flagged = True
        if flagged:
            findings.append({"line": line})
    return {"findings": findings}


def _scanner_source(fn: object) -> str:
    """Dedent a reference handler function to a sandbox-loadable `handle` source."""
    src = textwrap.dedent(inspect.getsource(fn))
    # The sandbox loads a symbol named `handle`; alias the reference function name.
    return src + f"\nhandle = {fn.__name__}\n"  # type: ignore[attr-defined]


_REFERENCE_SCANNERS = {
    "hardcoded-localhost-url": _scanner_source(_reference_localhost_url_handle),
    "hardcoded-topic-string": _scanner_source(_reference_topic_handle),
    "todo-fixme-marker": _scanner_source(_reference_todo_handle),
    "doc-content-scan": _scanner_source(_reference_doc_content_handle),
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_REFERENCE_SCANNERS))
def test_longtail_corpus_accepts_a_correct_reference_scanner(name: str) -> None:
    # The corpus must be satisfiable by a correct hand-authored scanner — proof
    # the corpus is well-specified, not merely unsatisfiable.
    result = evaluate_corpus_acceptance(_REFERENCE_SCANNERS[name], CORPORA[name])
    assert result.checked is True, name
    assert result.passed is True, f"{name}: reference scanner failed: {result.errors}"
    assert result.errors == [], name


@pytest.mark.unit
def test_doc_content_corpus_meets_fixture_density_dod() -> None:
    # DoD (OMN-13568): >= the IP-validator fixture density, with >= 4 adversarial
    # mutation cases on EACH side. The IP corpus is the explicit baseline.
    doc = CORPORA["doc-content-scan"]
    ip = CORPORA["hardcoded-private-ip"]
    assert len(doc.violation_fixtures) >= len(ip.violation_fixtures)
    assert len(doc.clean_fixtures) >= len(ip.clean_fixtures)
    doc_violation_mutations = [f for f in doc.violation_fixtures if f.mutation_of]
    doc_clean_mutations = [f for f in doc.clean_fixtures if f.mutation_of]
    assert len(doc_violation_mutations) >= 4, "need >=4 adversarial violation mutations"
    assert len(doc_clean_mutations) >= 4, "need >=4 adversarial clean mutations"


@pytest.mark.unit
def test_doc_content_corpus_rejects_an_omn_only_scanner() -> None:
    # A scanner that only flags OMN-XXXX refs misses every local-env leak (LAN IP,
    # host shorthand, personal path, ssh/email) — exactly the silent false-negative
    # the corpus exists to catch. It must be rejected.
    omn_only = (
        "def handle(input_data):\n"
        "    import re\n"
        "    s = input_data.get('source', '')\n"
        "    hits = [{'m': m.group()} for m in re.finditer(r'\\bOMN-\\d+\\b', s)]\n"
        "    return {'findings': hits}\n"
    )
    result = evaluate_corpus_acceptance(omn_only, CORPORA["doc-content-scan"])
    assert result.checked is True
    assert result.passed is False
    assert result.errors
