# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan task TE.1 ground-truth labels over the T0.2 replay cases (OMN-21138).

The form tests read the committed labels only. ``check_labels`` is the span check: given the pinned
transcript chains it reports every label whose normalised substring at its offsets differs from the
label. The unit tests prove it on synthetic chains; the same function runs against the replay bundle
on the lab host that holds it (``check_labels_against_root``).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

LABELS_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "context_compaction"
    / "labels.yaml"
)
KINDS = frozenset({"ruling", "correction", "constraint", "decision"})
POLARITIES = frozenset({"affirm", "negate"})
CONTROL_KINDS = frozenset(
    {"partial_quote", "hypothetical", "question", "corrected_ruling"}
)


def _load_labels() -> dict[str, Any]:
    labels: dict[str, Any] = yaml.safe_load(LABELS_PATH.read_text(encoding="utf-8"))
    return labels


LABELS: dict[str, Any] = _load_labels()


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def span_sha256(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def spanned_labels(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Every label of a case that points at a span (the corrected-ruling pairs do not)."""
    items = [*case["propositions"], *case["negative_segments"]]
    items += [c for c in case["negative_controls"] if "event_id" in c]
    return items


def check_labels(
    labels: dict[str, Any], chains: dict[str, list[dict[str, Any]]]
) -> list[str]:
    """Return one failure line per label whose span differs from its pinned record."""
    failures: list[str] = []
    for case in labels["cases"]:
        rows = chains.get(case["case_id"])
        if rows is None:
            failures.append(f"{case['case_id']}: no chain")
            continue
        by_key = {
            (r["event_id"], int(r["chunk_index"])): r for r in rows if "content" in r
        }
        for label in spanned_labels(case):
            row = by_key.get((label["event_id"], label["chunk_index"]))
            if row is None:
                failures.append(f"{label['label_id']}: record not in the chain")
                continue
            text = row["content"][label["start"] : label["end"]]
            if span_sha256(text) != label["span_sha256"]:
                failures.append(f"{label['label_id']}: span sha256 differs")
            if label["quote"] is not None and normalise(text) != normalise(
                label["quote"]
            ):
                failures.append(f"{label['label_id']}: quote differs at its offsets")
            for other in label.get("same_content_event_ids", []):
                twin = by_key.get((other, label["chunk_index"]))
                if twin is None or twin["content"] != row["content"]:
                    failures.append(
                        f"{label['label_id']}: {other} is not a same-content record"
                    )
    return failures


def check_labels_against_root(labels: dict[str, Any], root: Path) -> list[str]:
    """Run ``check_labels`` over the replay bundle at ``root`` (``ONEX_CONTEXT_REPLAY_ROOT``)."""
    chains: dict[str, list[dict[str, Any]]] = {}
    failures: list[str] = []
    for case in labels["cases"]:
        path = root / case["case_id"] / "transcript_chain.jsonl"
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != case["transcript_chain_sha256"]:
            failures.append(f"{case['case_id']}: transcript chain sha256 mismatch")
        chains[case["case_id"]] = [
            json.loads(line) for line in data.decode("utf-8").splitlines() if line
        ]
    return failures + check_labels(labels, chains)


def _all_label_ids(case: dict[str, Any]) -> set[str]:
    return {
        item["label_id"]
        for key in ("propositions", "negative_segments", "negative_controls")
        for item in case[key]
    }


def _synthetic_chains(labels: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Build records that hold each quoted label at its offsets, padded with spaces."""
    chains: dict[str, list[dict[str, Any]]] = {}
    for case in labels["cases"]:
        content: dict[tuple[str, int], list[str]] = {}
        for label in spanned_labels(case):
            if label["quote"] is None:
                continue
            key = (label["event_id"], label["chunk_index"])
            chars = content.setdefault(key, [])
            chars.extend(" " * max(0, label["end"] - len(chars)))
            chars[label["start"] : label["end"]] = list(label["quote"])
            for other in label.get("same_content_event_ids", []):
                content[(other, label["chunk_index"])] = chars
        chains[case["case_id"]] = [
            {"event_id": eid, "chunk_index": str(chunk), "content": "".join(chars)}
            for (eid, chunk), chars in content.items()
        ]
    return chains


def _quoted_only(labels: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(labels))
    for case in out["cases"]:
        for key in ("propositions", "negative_segments", "negative_controls"):
            case[key] = [
                item
                for item in case[key]
                if item.get("quote") is not None or "event_id" not in item
            ]
    return out


def test_labels_declare_the_scoring_before_any_model_run() -> None:
    assert LABELS["schema"] == "omnimarket/context_compaction_labels/1"
    assert LABELS["authored_before_model_output"] is True
    scoring = LABELS["scoring"]
    assert scoring["written_before_any_model_run"] is True
    assert set(scoring["kinds"]) == KINDS
    assert set(scoring["polarities"]) == POLARITIES


def test_every_case_labels_its_incident_and_has_negative_segments() -> None:
    assert [c["case_id"] for c in LABELS["cases"]] == [
        "L1-compact-mod-not-hooks",
        "L2-merge-queue-reasked",
        "L6-foreground-two-boundaries",
    ]
    for case in LABELS["cases"]:
        incidents = [p for p in case["propositions"] if p["t02_statement_id"]]
        assert incidents, case["case_id"]
        assert case["negative_segments"], case["case_id"]
        assert re.fullmatch(r"[0-9a-f]{64}", case["transcript_chain_sha256"])


def test_every_negative_control_kind_has_an_instance() -> None:
    kinds = {
        control["control_kind"]
        for case in LABELS["cases"]
        for control in case["negative_controls"]
    }
    assert kinds == CONTROL_KINDS


def test_labels_are_well_formed() -> None:
    for case in LABELS["cases"]:
        ids = _all_label_ids(case)
        props = {p["label_id"]: p for p in case["propositions"]}
        for prop in case["propositions"]:
            assert prop["kind"] in KINDS
            assert prop["polarity"] in POLARITIES
            assert [c["boundary"] for c in prop["ledger_coverage"]] == list(
                range(1, case["boundaries"] + 1)
            )
            if "corrects" in prop:
                assert prop["kind"] == "correction"
                assert prop["corrects"] in props
        for control in case["negative_controls"]:
            if control["control_kind"] == "corrected_ruling":
                assert props[control["correction"]]["corrects"] == control["ruling"]
            else:
                assert control["controls"] in ids
        for label in spanned_labels(case):
            assert 0 <= label["start"] < label["end"]
            assert re.fullmatch(r"[0-9a-f]{64}", label["span_sha256"])
            if label["quote"] is None:
                assert label["withheld_reason"]
            else:
                assert len(label["quote"]) == label["end"] - label["start"]
                assert span_sha256(label["quote"]) == label["span_sha256"]


def test_partial_quote_controls_sit_inside_the_span_they_control() -> None:
    for case in LABELS["cases"]:
        props = {p["label_id"]: p for p in case["propositions"]}
        for control in case["negative_controls"]:
            if control["control_kind"] != "partial_quote":
                continue
            prop = props[control["controls"]]
            assert control["event_id"] == prop["event_id"]
            assert prop["start"] <= control["start"] < control["end"] <= prop["end"]
            assert (control["start"], control["end"]) != (prop["start"], prop["end"])


def test_check_labels_passes_on_a_chain_holding_every_quote() -> None:
    quoted = _quoted_only(LABELS)
    assert check_labels(quoted, _synthetic_chains(quoted)) == []


def test_check_labels_reports_one_changed_character() -> None:
    quoted = _quoted_only(LABELS)
    chains = _synthetic_chains(quoted)
    label = quoted["cases"][1]["propositions"][0]
    for row in chains[quoted["cases"][1]["case_id"]]:
        if row["event_id"] == label["event_id"]:
            text = row["content"]
            row["content"] = text[: label["start"]] + "Y" + text[label["start"] + 1 :]
    failures = check_labels(quoted, chains)
    assert f"{label['label_id']}: span sha256 differs" in failures
    assert f"{label['label_id']}: quote differs at its offsets" in failures


def test_check_labels_reports_a_missing_record_and_chain() -> None:
    quoted = _quoted_only(LABELS)
    chains = _synthetic_chains(quoted)
    first = quoted["cases"][0]["propositions"][0]
    chains[quoted["cases"][0]["case_id"]] = [
        row
        for row in chains[quoted["cases"][0]["case_id"]]
        if row["event_id"] != first["event_id"]
    ]
    del chains[quoted["cases"][2]["case_id"]]
    failures = check_labels(quoted, chains)
    assert f"{first['label_id']}: record not in the chain" in failures
    assert f"{quoted['cases'][2]['case_id']}: no chain" in failures
