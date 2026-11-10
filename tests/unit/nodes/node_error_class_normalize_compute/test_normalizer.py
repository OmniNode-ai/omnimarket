# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Volatile subjects cannot split a class; distinct causes cannot collapse."""

import hashlib
import json
from pathlib import Path

import pytest

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorEvent,
    ModelErrorNormalizerFacts,
    ModelErrorSubject,
)
from omnimarket.nodes.node_error_class_normalize_compute import (
    NodeErrorClassNormalizeCompute,
)
from omnimarket.nodes.node_error_class_normalize_compute.overlay import (
    load_error_normalizer_facts,
)


def test_same_class_one_key() -> None:
    facts = load_error_normalizer_facts(
        explicit={
            "configuration": {
                "strip_stages": [
                    {
                        "name": "shas",
                        "pattern": r"\b[0-9a-f]{7,40}\b",
                        "replacement": "<sha>",
                    },
                    {"name": "hosts", "kind": "hosts", "replacement": "<host>"},
                ],
                "producer_adapters": [
                    {"source": "gate", "producer": "handoff", "unique_codes": True}
                ],
                "lane_name_patterns": [],
                "max_open_triage": 2,
            },
            "hosts": ["lab-alpha", "lab-beta"],
        }
    )
    keys = [
        NodeErrorClassNormalizeCompute()
        .handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source="gate", producer="handoff", code="NOT-READY", text=text
                ),
                facts=facts,
            )
        )
        .class_key
        for text in [
            "NOT-READY install abcdef123 on lab-alpha",
            "NOT-READY install 1234567ab on lab-beta",
        ]
    ]
    assert keys[0] == keys[1]


def test_hook_refusal_calls_normalizer_and_keeps_lane_out_of_class_key() -> None:
    from omnimarket.nodes.node_hook_refusal_row_compute.handlers import (
        HandlerHookRefusalRowCompute,
    )
    from omnimarket.nodes.node_hook_refusal_row_compute.models import (
        ModelHookRefusalRowRequest,
    )

    facts = fixture_facts()
    results = [
        HandlerHookRefusalRowCompute().handle(
            ModelHookRefusalRowRequest(
                guard="worktree-guard",
                reason="refused path /tmp/worktree",
                lane=lane,
                timestamp="2026-11-04T12:00:00Z",
                normalizer_facts=facts,
            )
        )
        for lane in ("lane-alpha", "lane-beta")
    ]
    assert results[0].class_key == results[1].class_key
    assert results[0].key_version == results[1].key_version
    assert results[0].key != results[1].key
    assert f"class_key={results[0].class_key}" in results[0].row


def fixture_facts():
    from pathlib import Path

    return load_error_normalizer_facts(
        explicit=json.loads(
            (Path(__file__).with_name("fixtures") / "normalizer_facts.json").read_text()
        )
    )


@pytest.mark.parametrize(
    ("source", "producer", "code", "left", "right"),
    [
        (
            "tool",
            "Bash",
            "1",
            "permission denied opening file",
            "network connection refused",
        ),
        (
            "ci",
            "validator-tests",
            "failure",
            "database schema mismatch",
            "runner out of memory",
        ),
        (
            "gate",
            "handoff",
            "NOT-READY",
            "NOT-READY install current head",
            "NOT-READY missing passing evidence",
        ),
        (
            "tool",
            "Bash",
            "1",
            "failed before publishing because authentication expired",
            "failed before publishing because disk exhausted",
        ),
        (
            "lane",
            "remote-lane",
            "blocked",
            "brief premise absent",
            "owner repository unreachable",
        ),
    ],
)
def test_distinct_causes_distinct_keys(source, producer, code, left, right):
    facts = fixture_facts()
    node = NodeErrorClassNormalizeCompute()
    results = [
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source=source, producer=producer, code=code, text=text
                ),
                facts=facts,
            )
        )
        for text in (left, right)
    ]
    assert results[0].class_key != results[1].class_key
    for result in results:
        assert result.class_key.endswith(
            hashlib.sha256(result.template.encode()).hexdigest()[:10]
        )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("install https://a.example/ref/123", "install https://b.example/ref/456"),
        ("head abcdef1", "head 9876543abcdef"),
        (
            "request 133acde0-1abc-4444-8def-77abc1238888",
            "request 233bcde0-2bcd-5555-9bcd-88bcd2349999",
        ),
        ("at 2026-11-04T12:34:56Z", "at 2026-11-03T01:02:03.123+00:00"),
        ("open /tmp/one/file", "open /work/two/other"),
        ("ticket ABC-123", "ticket ABC-456"),
        ("PR #123", "PR #456"),
        ("package#123", "other#456"),
        ("host lab-alpha", "host lab-beta"),
        ("lane work-one-1234", "lane other-45-5678"),
        ("waited 30 seconds", "waited 80 seconds"),
    ],
)
def test_same_class_one_key_volatile_stages(left, right):
    node = NodeErrorClassNormalizeCompute()
    keys = [
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source="gate", producer="handoff", code="NOT-READY", text=text
                ),
                facts=fixture_facts(),
            )
        ).class_key
        for text in (left, right)
    ]
    assert keys[0] == keys[1]


def test_subject_never_enters_key():
    node = NodeErrorClassNormalizeCompute()
    results = [
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source="tool",
                    producer="Bash",
                    code="1",
                    text="permission denied",
                    subject=ModelErrorSubject(
                        repository=name,
                        pr=name,
                        head=name,
                        host=name,
                        lane=name,
                        ticket=name,
                        time=name,
                    ),
                ),
                facts=fixture_facts(),
            )
        )
        for name in ("alpha", "beta")
    ]
    assert results[0] == results[1]


def test_replay_preserves_old_keys_across_host_registry_change():
    directory = Path(__file__).with_name("fixtures")
    old = ModelErrorNormalizerFacts.model_validate_json(
        (directory / "hosts_old.json").read_text()
    )
    new = ModelErrorNormalizerFacts.model_validate_json(
        (directory / "hosts_new.json").read_text()
    )
    event = ModelErrorEvent(
        source="gate", producer="handoff", code="NOT-READY", text="install on lab-beta"
    )
    node = NodeErrorClassNormalizeCompute()
    before = node.handle(ModelErrorClassNormalizeRequest(event=event, facts=old))
    after = node.handle(ModelErrorClassNormalizeRequest(event=event, facts=new))
    assert before.key_version != after.key_version
    assert before.class_key != after.class_key
    assert (
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=event, facts=old, key_version=before.key_version
            )
        )
        == before
    )
    assert (
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=event, facts=new, key_version=after.key_version
            )
        )
        == after
    )
    with pytest.raises(ValueError, match="key_version"):
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=event, facts=new, key_version=before.key_version
            )
        )


def test_producer_unique_code_declaration_controls_identity():
    node = NodeErrorClassNormalizeCompute()
    for source, code, expected in [
        ("tool", "1", "template"),
        ("ci", "failure", "template"),
        ("lane", "blocked", "template"),
        ("hook", "sanctioned-root", "sanctioned-root"),
        ("tick", "host-unread", "host-unread"),
    ]:
        result = node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source=source, producer="producer", code=code, text="a cause"
                ),
                facts=fixture_facts(),
            )
        )
        assert result.identity == expected


def test_prose_is_only_provisional_deduplication_after_stripping():
    node = NodeErrorClassNormalizeCompute()
    results = [
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source="prose", producer="ledger", text_kind="prose", text=text
                ),
                facts=fixture_facts(),
            )
        )
        for text in (
            "could not read /work/alpha",
            "could not read /work/beta",
            "could not publish /work/beta",
        )
    ]
    assert all(result.provisional for result in results)
    assert results[0].class_key.startswith("prose.ledger.")
    assert results[0].class_key == results[1].class_key
    assert results[0].class_key != results[2].class_key


def test_overlay_reader_requires_explicit_configuration_and_hosts(tmp_path):
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError

    with pytest.raises(NodeOverlayError):
        load_error_normalizer_facts(environ={})
    target = tmp_path / "error_normalizer"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        fixture_facts().configuration.model_dump_json()
    )
    with pytest.raises(NodeOverlayError, match="host"):
        load_error_normalizer_facts(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_handler_reads_no_environment_file_clock_or_host_registry(monkeypatch):
    import builtins
    import os

    facts = fixture_facts()

    def refused(*args, **kwargs):
        raise AssertionError("compute attempted runtime I/O")

    monkeypatch.setattr(builtins, "open", refused)
    monkeypatch.setattr(Path, "read_text", refused)
    monkeypatch.setattr(os, "getenv", refused)
    result = NodeErrorClassNormalizeCompute().handle(
        ModelErrorClassNormalizeRequest(
            event=ModelErrorEvent(
                source="tool", producer="Bash", text="permission denied"
            ),
            facts=facts,
        )
    )
    assert result.class_key


def test_same_class_one_key_hook_reason_with_volatile_lane_and_host():
    from omnimarket.nodes.node_hook_refusal_row_compute.handlers import (
        HandlerHookRefusalRowCompute,
    )
    from omnimarket.nodes.node_hook_refusal_row_compute.models import (
        ModelHookRefusalRowRequest,
    )

    results = [
        HandlerHookRefusalRowCompute().handle(
            ModelHookRefusalRowRequest(
                normalizer_facts=fixture_facts(),
                guard="guard",
                timestamp="2026-11-04T12:00:00Z",
                reason=reason,
            )
        )
        for reason in (
            "held by lane work-one-1234 on lab-alpha",
            "held by lane work-two-5678 on lab-beta",
        )
    ]
    assert results[0].class_key == results[1].class_key


def test_prose_alias_resolves_to_canonical_or_explicit_none_without_changing_raw_key():
    from omnimarket.models.error_normalizer import ModelErrorClassAlias

    request = ModelErrorClassNormalizeRequest(
        event=ModelErrorEvent(
            source="prose", producer="ledger", text_kind="prose", text="cannot proceed"
        ),
        facts=fixture_facts(),
    )
    node = NodeErrorClassNormalizeCompute()
    original = node.handle(request)
    resolved = node.handle(
        request.model_copy(
            update={
                "aliases": (
                    ModelErrorClassAlias(
                        provisional_key=original.class_key,
                        canonical_key="gate.handoff.not-ready.0123456789",
                    ),
                )
            }
        )
    )
    ignored = node.handle(
        request.model_copy(
            update={
                "aliases": (
                    ModelErrorClassAlias(
                        provisional_key=original.class_key, canonical_key=None
                    ),
                )
            }
        )
    )
    assert resolved.class_key == original.class_key == ignored.class_key
    assert resolved.key_version == original.key_version == ignored.key_version
    assert resolved.canonical_key == "gate.handoff.not-ready.0123456789"
    assert resolved.alias_state == "canonical"
    assert ignored.canonical_key is None
    assert ignored.alias_state == "none"
    assert original.alias_state == "unresolved"


def test_overlay_reader_combines_configuration_with_caller_host_snapshot(tmp_path):
    target = tmp_path / "error_normalizer"
    target.mkdir()
    facts = fixture_facts()
    (target / "overlay.yaml").write_text(facts.configuration.model_dump_json())
    loaded = load_error_normalizer_facts(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
        hosts=facts.hosts,
    )
    assert loaded == facts


def test_unique_producer_codes_survive_number_and_ticket_stripping():
    node = NodeErrorClassNormalizeCompute()
    results = [
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source="gate", producer="gate", code=code, text="request failed"
                ),
                facts=fixture_facts(),
            )
        )
        for code in ("ERR-1234", "ERR-5678")
    ]
    assert results[0].identity == "ERR-1234"
    assert results[0].class_key != results[1].class_key


def test_same_class_one_key_relative_paths():
    node = NodeErrorClassNormalizeCompute()
    keys = [
        node.handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(source="tool", producer="Bash", text=text),
                facts=fixture_facts(),
            )
        ).class_key
        for text in ("cannot open src/alpha/file.py", "cannot open tests/beta/other.py")
    ]
    assert keys[0] == keys[1]
