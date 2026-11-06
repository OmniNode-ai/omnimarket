# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pinned, scrubbed evidence: absent or changed rows fail replay."""

import hashlib
import json
from collections import Counter
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures")


def corpus_rows():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    return [
        json.loads(line)
        for item in manifest["corpus_files"]
        for line in (FIXTURES / item["path"]).read_text().splitlines()
    ]


def test_corpus_digest() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert {p.name for p in FIXTURES.glob("corpus-*.jsonl")} == {
        item["path"] for item in manifest["corpus_files"]
    }
    chunks = []
    for item in manifest["corpus_files"]:
        content = (FIXTURES / item["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item["sha256"], item["path"]
        assert len(content.splitlines()) == item["row_count"]
        chunks.append(content)
    content = b"".join(chunks)
    assert hashlib.sha256(content).hexdigest() == manifest["corpus_sha256"]
    rows = [json.loads(line) for line in content.splitlines()]
    assert len(rows) == manifest["row_count"]
    assert dict(Counter(row["kind"] for row in rows)) == manifest["counts_by_kind"]
    assert len({row["id"] for row in rows}) == len(rows)
    for name, digest in manifest["fixture_digests"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, (
            name
        )


def test_misses_have_labels() -> None:
    cases = json.loads((FIXTURES / "acceptance.json").read_text())
    assert {c["id"] for c in cases["misses"]} == {"1a", "1b", "2", "3", "4", "5"}
    for case in cases["misses"]:
        if case["id"] == "5":
            assert case["status"] == "pending_seed_overlay"
            assert case["selection_rule"]
            continue
        assert case["status"] == "captured", case["id"]
        assert case["records"], case["id"]
        assert case["gold_label"], case["id"]
        assert case["applies"] is True
    claim = next(c for c in cases["misses"] if c["id"] == "3")
    assert claim["negative_records"]
    assert claim["negative_applies"] is False


def test_corpus_unmeasured_breaks_cannot_claim_acceptance() -> None:
    cases = json.loads((FIXTURES / "acceptance.json").read_text())
    assert {c["id"] for c in cases["breaks"]} == {f"B{i}" for i in range(1, 8)}
    unmeasured = [
        c["id"] for c in cases["breaks"] if not c["records"] or not c["gold_label"]
    ]
    assert unmeasured == cases["unmeasured"] == ["B5", "B6"]
    assert not cases["acceptance_passed"]
    for case in cases["breaks"]:
        assert (case["status"] == "captured") == bool(
            case["records"] and case["gold_label"]
        )


def test_corpus_baseline_replays_existing_normalizer() -> None:
    from omnimarket.nodes.node_hook_refusal_row_compute.handlers.handler_hook_refusal_row import (
        dedupe_key,
        normalise_reason,
    )

    counts = Counter()
    for row in corpus_rows():
        cells = dict(x.split("=", 1) for x in row["row"].split(" | ")[2:] if "=" in x)
        counts[
            dedupe_key(
                cells.get("guard", row["event"]["producer"]),
                normalise_reason(row["event"]["text"]),
                cells.get("lane", cells.get("from", "")),
            )
        ] += 1
    baseline = json.loads((FIXTURES / "manifest.json").read_text())["baseline"][
        "bare_existing_normalizer"
    ]
    assert len(counts) == baseline["key_count"]
    assert sum(n == 1 for n in counts.values()) == baseline["singleton_count"]


def test_corpus_replay_keys_are_pinned() -> None:
    from omnimarket.models.error_normalizer import (
        ModelErrorClassNormalizeRequest,
        ModelErrorEvent,
        ModelErrorNormalizerFacts,
    )
    from omnimarket.nodes.node_error_class_normalize_compute import (
        NodeErrorClassNormalizeCompute,
    )

    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    baseline = manifest["baseline"]["new_normalizer"]
    facts = ModelErrorNormalizerFacts.model_validate_json(
        (FIXTURES / "normalizer_facts.json").read_text()
    )
    counts = Counter()
    node = NodeErrorClassNormalizeCompute()
    for row in corpus_rows():
        result = node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent.model_validate(row["event"]),
                facts=facts,
                key_version=baseline["key_version"],
            )
        )
        counts[result.class_key] += 1
    assert len(counts) == baseline["key_count"]
    assert sum(n == 1 for n in counts.values()) == baseline["singleton_count"]


def test_same_class_one_key_corpus_gold_groups() -> None:
    from omnimarket.models.error_normalizer import (
        ModelErrorClassNormalizeRequest,
        ModelErrorEvent,
        ModelErrorNormalizerFacts,
    )
    from omnimarket.nodes.node_error_class_normalize_compute import (
        NodeErrorClassNormalizeCompute,
    )

    groups = json.loads((FIXTURES / "class_groups.json").read_text())
    facts = ModelErrorNormalizerFacts.model_validate_json(
        (FIXTURES / "normalizer_facts.json").read_text()
    )
    node = NodeErrorClassNormalizeCompute()
    for group in groups["same_class"]:
        assert len(group["events"]) >= 2
        keys = {
            node.handle(
                ModelErrorClassNormalizeRequest(
                    event=ModelErrorEvent.model_validate(event), facts=facts
                )
            ).class_key
            for event in group["events"]
        }
        assert len(keys) == 1, group["label"]


def test_distinct_causes_distinct_keys_corpus_gold_groups() -> None:
    from omnimarket.models.error_normalizer import (
        ModelErrorClassNormalizeRequest,
        ModelErrorEvent,
        ModelErrorNormalizerFacts,
    )
    from omnimarket.nodes.node_error_class_normalize_compute import (
        NodeErrorClassNormalizeCompute,
    )

    groups = json.loads((FIXTURES / "class_groups.json").read_text())
    facts = ModelErrorNormalizerFacts.model_validate_json(
        (FIXTURES / "normalizer_facts.json").read_text()
    )
    node = NodeErrorClassNormalizeCompute()
    for group in groups["distinct_causes"]:
        keys = {
            node.handle(
                ModelErrorClassNormalizeRequest(
                    event=ModelErrorEvent.model_validate(event), facts=facts
                )
            ).class_key
            for event in group["events"]
        }
        assert len(keys) == len(group["labels"]), group["labels"]
