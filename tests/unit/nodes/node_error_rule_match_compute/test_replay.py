# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay uses pinned upstream facts and distinguishes unlabelled false matches."""

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorEvent,
)
from omnimarket.models.error_rule_match import (
    ModelErrorRuleReplayRequest,
)
from omnimarket.nodes.node_error_class_normalize_compute import (
    NodeErrorClassNormalizeCompute,
)
from omnimarket.nodes.node_error_rule_replay_compute import NodeErrorRuleReplayCompute

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = Path(__file__).parent.parent / "node_error_class_normalize_compute/fixtures"


def corpus_request():
    records = []
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    for chunk in manifest["corpus_files"]:
        for line in (CORPUS / chunk["path"]).read_text().splitlines():
            row = json.loads(line)
            records.append({"id": row["id"], "event": row["event"]})
    return ModelErrorRuleReplayRequest.model_validate(
        {
            "records": records,
            "table": {
                "rows": json.loads((FIXTURES / "seed_snapshot.json").read_text())[
                    "error_rules"
                ],
                "key_versions": json.loads(
                    (FIXTURES / "key_versions.json").read_text()
                ),
            },
            "facts": json.loads((CORPUS / "normalizer_facts.json").read_text()),
            "key_version": manifest["baseline"]["new_normalizer"]["key_version"],
        }
    )


def test_corpus_replay_is_deterministic():
    request = corpus_request()
    node = NodeErrorRuleReplayCompute()
    first = node.handle(request)
    second = node.handle(
        request.model_copy(update={"records": tuple(reversed(request.records))})
    )
    assert first.model_dump_json() == second.model_dump_json()
    assert first.report() == second.report()
    assert first.hits + first.misses == len(request.records)
    assert first.unlabelled == len(request.records)
    assert first.labelled == 0
    assert first.no_match_keys == tuple(sorted(set(first.no_match_keys)))


def test_replay_counts_labelled_false_matches_and_prints_keys():
    request = corpus_request()
    known = {"source": "tool", "producer": "Bash", "text": "known fixture cause"}
    context = {"host_role": "lab", "repository": "omnimarket", "ticket_state": "open"}
    request = ModelErrorRuleReplayRequest.model_validate(
        {
            **request.model_dump(mode="json"),
            "records": [
                {
                    "id": "hit",
                    "event": known,
                    "context": context,
                    "gold": "MATCH",
                    "expected_row_id": "fixture-known",
                },
                {"id": "false", "event": known, "context": context, "gold": "NO-MATCH"},
                {
                    "id": "miss",
                    "event": {**known, "text": "different fixture cause"},
                    "gold": "NO-MATCH",
                },
            ],
        }
    )
    result = NodeErrorRuleReplayCompute().handle(request)
    assert (
        result.hits,
        result.misses,
        result.false_matches,
        result.labelled,
        result.unlabelled,
    ) == (2, 1, 1, 3, 0)
    assert result.report().startswith(
        "hits=2 misses=1 false_matches=1 labelled=3 unlabelled=0\n"
    )
    assert len(result.no_match_keys) == 1
    assert f"NO-MATCH {result.no_match_keys[0]}\n" in result.report()


def test_fresh_class_absent_from_frozen_seed_digest():
    frozen = json.loads((FIXTURES / "miss_5.json").read_text())
    raw = (FIXTURES / "seed_snapshot.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == frozen["seed_sha256"]
    from tests.unit.nodes.node_error_rule_match_compute.test_match import (
        table as fixture_table,
    )

    table = fixture_table()
    assert all(frozen["class_key"] not in row.class_keys for row in table.rows)
    request = corpus_request()
    normalizer = NodeErrorClassNormalizeCompute()
    grouped = defaultdict(list)
    for record in request.records:
        key = normalizer.handle(
            ModelErrorClassNormalizeRequest(event=record.event, facts=request.facts)
        ).class_key
        if not any(key in row.class_keys for row in table.rows):
            grouped[key].append(record.id)
    winner = min(grouped, key=lambda key: (-len(grouped[key]), key))
    assert winner == frozen["class_key"]
    assert sorted(grouped[winner]) == frozen["member_ids"]
    normalized = normalizer.handle(
        ModelErrorClassNormalizeRequest(
            event=ModelErrorEvent.model_validate(frozen["event"]), facts=request.facts
        )
    )
    assert normalized.key_version == frozen["key_version"]
    assert normalized.class_key == winner
