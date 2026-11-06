# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Promise capture regressions through the typed node boundary."""

from datetime import UTC, datetime
from importlib.resources import files

import pytest
import yaml

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseClassifyRequest,
    ModelPromisePolicy,
)
from omnimarket.models.operator_capture import ModelUtterance
from omnimarket.nodes.node_orchestrator_promise_classify_compute.handlers.handler_promise_classify import (
    HandlerPromiseClassify,
)

pytestmark = pytest.mark.unit


def policy(**updates: object) -> ModelPromisePolicy:
    raw = yaml.safe_load(
        files("omnimarket.nodes.node_orchestrator_promise_classify_compute")
        .joinpath("contract.yaml")
        .read_text()
    )["config"]
    raw.update(updates)
    return ModelPromisePolicy.model_validate(raw)


def classify(text: str, **kwargs: object):
    utterance = ModelUtterance(
        capture_id="stop-a",
        text=text,
        session_id="session-a",
        source="content-captured",
        lane="orchestrator-example",
        captured_at=datetime(2026, 11, 4, 12, tzinfo=UTC),
    )
    return HandlerPromiseClassify().handle(
        ModelPromiseClassifyRequest(
            utterance=utterance,
            policy=policy(),
            **kwargs,
        )
    )


def test_promise_classify_install_branch_emits_promise_and_disposition() -> None:
    quote = "I will install the branch on h051 and read it back"
    result = classify(quote)
    assert len(result.items) == 1
    assert result.items[0].kind == "promise"
    assert result.items[0].quote == quote
    assert result.items[0].owner == "orchestrator-example"
    assert result.items[0].state == "candidate"
    assert result.dispositions[0].item_id == result.items[0].item_id
    assert result.dispositions[0].quote == quote


def test_promise_classify_effect_writes_candidate_and_replay_writes_none(
    tmp_path,
) -> None:
    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    handler = build_handler(store_path=tmp_path / "promises.sqlite", policy=policy())
    record = ModelPromiseCaptureRecord(
        session_id="session-a",
        content_kind="assistant_reply",
        content_record_id="stop-a",
        lane="orchestrator-example",
        emitted_at="2026-11-04T12:00:00Z",
        content="I will install the branch on h051 and read it back",
    )
    result = handler.handle(record)
    assert result.status == "recorded"
    assert len([row for row in result.rows if " | kind=promise | " in row]) == 1
    assert " | state=candidate | " in result.rows[0]
    assert result.candidate_count == 1
    assert result.ledger_rows == ()
    assert handler.handle(record).rows == ()
    assert handler.handle(record).status == "duplicate"


@pytest.mark.parametrize(
    "text",
    [
        "Tick: I will install the branch.",
        "Tool echo: I will install the branch.",
        "<bash-stdout>I will install the branch.</bash-stdout>",
        "<tool_result>I will install the branch.</tool_result>",
        "2026-11-04T12:00:00Z | STATUS | lane=orchestrator | I will install it",
        '{"tool_name": "echo", "content": "I will install it"}',
    ],
)
def test_promise_classify_machine_reply_has_no_items_or_dispositions(text) -> None:
    result = classify(text)
    assert result.machine
    assert result.items == result.dispositions == ()


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("I will install it. Thanks. I\u2019ll read it back. Good night.", 2),
        ("I will install version 4.5. Thanks.", 1),
        ("I installed it. The worker will retry automatically.", 0),
        ('You said "I will install it". The tool said I will do it.', 0),
        ("I might install it. I plan to read it back.", 2),
        ("I'll check it. We will install it. We're done.", 2),
        ("If the slot opens, I will install it.", 1),
    ],
)
def test_promise_classify_wording_and_every_sentence(text, count) -> None:
    result = classify(text)
    assert len(result.items) == count
    assert len(result.dispositions) == (text.count(".") if "4.5" not in text else 2)
    assert all(disposition.quote in text for disposition in result.dispositions)


def test_promise_classify_ten_commitments_and_repeated_words_get_distinct_ids() -> None:
    result = classify(" ".join(["I will check it."] * 10))
    assert len(result.items) == 10
    assert len({item.item_id for item in result.items}) == 10
    assert [disposition.item_id for disposition in result.dispositions] == [
        item.item_id for item in result.items
    ]
    assert classify(" ".join(["I will check it."] * 10)) == result


def delegated(texts, confidences=None):
    import json

    return json.dumps(
        {
            "items": [
                {
                    "sentence": index,
                    "kind": kind,
                    "quote": text,
                    "subject": "fixture",
                    "confidence": confidence,
                }
                for index, ((text, kind), confidence) in enumerate(
                    zip(texts, confidences or [0.95] * len(texts), strict=True), 1
                )
            ]
        }
    )


def test_promise_classify_accepts_whole_delegated_answer_and_keeps_low_confidence() -> (
    None
):
    result = classify(
        "I will install it. Thanks.",
        delegated_response=delegated(
            [
                ("I will install it.", "promise"),
                ("Thanks.", "chat"),
            ],
            [0.1, 0.99],
        ),
        delegated_model="fixture-model",
    )
    assert result.classifier.value == "delegated"
    assert result.classifier_model == "fixture-model"
    assert result.items[0].confidence == 0.1
    assert result.items[0].state == "candidate"
    assert result.items[0].owner == "orchestrator-example"
    assert result.low_confidence == 1
    assert result.dispositions[1].item_id is None


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        '{"items": []}',
        delegated([("I will install it.", "promise")]),
        delegated(
            [("I will install it.", "chat"), ("I will read it back.", "promise")]
        ),
        delegated(
            [("I will install it.", "promise"), ("Invented promise.", "promise")]
        ),
        delegated(
            [("I will install it.", "promise"), ("I will read it back.", "alien")]
        ),
        delegated(
            [("I will install it.", "promise"), ("I will read it back.", "promise")],
            [True, 0.9],
        ),
        delegated(
            [("I will install it.", "promise"), ("I will read it back.", "promise")],
            [0.9, -0.2],
        ),
        '```json\n{"items": []}\n```',
    ],
)
def test_promise_classify_invalid_delegated_answer_falls_back_whole(response) -> None:
    expected = classify("I will install it. I will read it back.")
    actual = classify(
        "I will install it. I will read it back.", delegated_response=response
    )
    assert actual.classifier.value == "heuristic"
    assert actual.items == expected.items
    assert actual.dispositions == expected.dispositions


def test_promise_classify_effect_ten_commitments_replay_and_restart(tmp_path) -> None:
    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    path = tmp_path / "promises.sqlite"
    record = ModelPromiseCaptureRecord(
        session_id="session-a",
        content_kind="assistant_reply",
        content_record_id="stop-ten",
        lane="orchestrator-example",
        emitted_at="2026-11-04T12:00:00Z",
        content=" ".join(["I will check it."] * 10),
    )
    handler = build_handler(store_path=path, policy=policy())
    result = handler.handle(record)
    promises = [row for row in result.rows if " | kind=promise | " in row]
    assert len(promises) == 10
    assert len(set(promises)) == 10
    assert len([row for row in result.rows if " | kind=disposition | " in row]) == 10
    assert result.candidate_count == 10
    assert handler.handle(record).rows == ()
    restarted = build_handler(store_path=path, policy=policy())
    assert restarted.handle(record).rows == ()
    assert restarted.handle(record).candidate_count == 10


def test_promise_classify_chunks_wait_for_complete_reply_and_survive_restart(
    tmp_path,
) -> None:
    import hashlib

    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    path = tmp_path / "promises.sqlite"
    text = "I will install it. I will read it back."
    record = ModelPromiseCaptureRecord(
        session_id="session-a",
        content_kind="assistant_reply",
        content_record_id="stop-chunks",
        lane="orchestrator-example",
        emitted_at="2026-11-04T12:00:00Z",
        content="I will install it. ",
        chunk_count=2,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    handler = build_handler(store_path=path, policy=policy())
    assert (
        handler.handle(
            record.model_copy(
                update={"content": "I will read it back.", "chunk_index": 1}
            )
        ).status
        == "pending"
    )
    assert handler.store.captures() == ()
    restarted = build_handler(store_path=path, policy=policy())
    result = restarted.handle(record)
    assert result.status == "recorded"
    assert len([row for row in result.rows if " | kind=promise | " in row]) == 2
    assert restarted.store.captures()[0].utterance.text == text
    assert restarted.handle(record).rows == ()


@pytest.mark.parametrize(
    ("updates", "status"),
    [
        ({"content_kind": "tool_response"}, "ignored"),
        ({"lane": "build-something"}, "ignored"),
        ({"lane": None}, "ignored"),
        ({"content": "Tick: I will install it."}, "ignored"),
        ({"truncated": True}, "pending"),
    ],
)
def test_promise_classify_effect_non_orchestrator_machine_and_truncated(
    tmp_path, updates, status
) -> None:
    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    handler = build_handler(store_path=tmp_path / "promises.sqlite", policy=policy())
    record = ModelPromiseCaptureRecord(
        session_id="session-a",
        content_kind="assistant_reply",
        content_record_id="stop-ignore",
        lane="orchestrator-example",
        emitted_at="2026-11-04T12:00:00Z",
        content="I will install it.",
    ).model_copy(update=updates)
    result = handler.handle(record)
    assert result.status == status
    assert result.rows == result.ledger_rows == ()
    assert handler.store.captures() == ()


def test_promise_classify_shadow_tick_prints_only_candidate_and_uncertainty_counts(
    tmp_path,
) -> None:
    from omnimarket.models.model_orchestrator_promise import (
        ModelPromiseCaptureRecord,
        ModelPromiseRenderRequest,
    )
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )
    from omnimarket.nodes.node_orchestrator_promise_classify_compute.handlers.handler_promise_render import (
        HandlerPromiseRender,
    )

    handler = build_handler(store_path=tmp_path / "promises.sqlite", policy=policy())
    handler.handle(
        ModelPromiseCaptureRecord(
            session_id="session-a",
            content_kind="assistant_reply",
            content_record_id="stop-low",
            lane="orchestrator-example",
            emitted_at="2026-11-04T12:00:00Z",
            content="I might install the private candidate marker.",
        )
    )
    captures = handler.store.captures()
    result = HandlerPromiseRender().handle(
        ModelPromiseRenderRequest(
            captures=(*captures, *captures),
            now=datetime(2026, 11, 4, 13, tzinfo=UTC),
        )
    )
    assert result.candidate_count == result.low_confidence_count == 1
    assert result.text == "Promise candidates: 1; low-confidence review: 1"
    assert "private candidate marker" not in result.text
    assert captures[0].classification.items[0].owner == "orchestrator-example"
    assert captures[0].ledger_rows == ()


def test_promise_classify_source_sentences_and_pipe_quotes_are_lossless(
    tmp_path,
) -> None:
    import json

    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    handler = build_handler(store_path=tmp_path / "promises.sqlite", policy=policy())
    text = 'I will check "a | b". Thanks. I will read it back.'
    result = handler.handle(
        ModelPromiseCaptureRecord(
            session_id="session-a",
            content_kind="assistant_reply",
            content_record_id="stop-pipe",
            lane="orchestrator-example",
            emitted_at="2026-11-04T12:00:00Z",
            content=text,
        )
    )
    dispositions = [row for row in result.rows if " | kind=disposition | " in row]
    assert len(dispositions) == 3
    assert " | item=chat | " in dispositions[1]
    assert json.loads(dispositions[0].split(" | ")[-1]) == 'I will check "a | b".'
    assert all(
        "capture=" in row and "source=content-captured:stop-pipe" in row
        for row in dispositions
    )
    assert [f"sentence={i}" in row for i, row in enumerate(dispositions, 1)] == [
        True
    ] * 3


def test_promise_classify_ten_explicit_commitments_in_one_sentence() -> None:
    text = " and ".join(["I will check it"] * 10) + "."
    result = classify(text)
    assert len(result.items) == 10
    assert len({item.item_id for item in result.items}) == 10
    assert len(result.dispositions) == 1
    assert result.dispositions[0].item_id == result.items[0].item_id
    assert all(item.quote in text for item in result.items)


def test_promise_classify_unsure_delegated_chat_is_retained_for_review() -> None:
    result = classify(
        "Taking care of this.",
        delegated_response=delegated(
            [
                ("Taking care of this.", "chat"),
            ],
            [0.2],
        ),
    )
    assert len(result.items) == 1
    assert result.items[0].state == "candidate"
    assert result.items[0].confidence == 0.2
    assert result.low_confidence == 1


def test_promise_classify_non_ascii_chat_still_has_a_disposition() -> None:
    result = classify("👍. I will check it.")
    assert len(result.dispositions) == 2
    assert result.dispositions[0].item_id is None
    assert result.dispositions[0].quote == "👍."


def test_promise_classify_stop_wire_without_lane_uses_overlay_session_binding(
    tmp_path,
) -> None:
    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    configured = policy(session_lanes={"captured-session": "orchestrator-bound"})
    handler = build_handler(store_path=tmp_path / "promises.sqlite", policy=configured)
    record = ModelPromiseCaptureRecord(
        session_id="captured-session",
        content_kind="assistant_reply",
        content_record_id="stop-real-wire",
        emitted_at="2026-11-04T12:00:00Z",
        content="I will install it.",
    )
    result = handler.handle(record)
    assert result.candidate_count == 1
    assert (
        handler.store.captures()[0].classification.items[0].owner
        == "orchestrator-bound"
    )
    assert result.rows


def test_promise_classify_bound_session_subagent_cannot_make_orchestrator_promises(
    tmp_path,
) -> None:
    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    configured = policy(session_lanes={"captured-session": "orchestrator-bound"})
    handler = build_handler(store_path=tmp_path / "promises.sqlite", policy=configured)
    record = ModelPromiseCaptureRecord(
        session_id="captured-session",
        content_kind="assistant_reply",
        content_record_id="stop-agent",
        emitted_at="2026-11-04T12:00:00Z",
        content="I will install it.",
        agent_id="child-agent",
    )
    assert handler.handle(record).rows == ()
    assert handler.store.captures() == ()


def test_promise_classify_uncertain_delegated_chat_cannot_lose_its_confidence() -> None:
    result = classify(
        "I will check it.",
        delegated_response=delegated(
            [
                ("I will check it.", "chat"),
            ],
            [0.2],
        ),
    )
    assert result.classifier.value == "delegated"
    assert result.items[0].confidence == 0.2
    assert result.low_confidence == 1


def test_promise_classify_legacy_record_source_is_a_digest_not_content(
    tmp_path,
) -> None:
    from omnimarket.models.model_orchestrator_promise import ModelPromiseCaptureRecord
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
        build_handler,
    )

    handler = build_handler(store_path=tmp_path / "promises.sqlite", policy=policy())
    handler.handle(
        ModelPromiseCaptureRecord(
            session_id="session-a",
            content_kind="assistant_reply",
            lane="orchestrator-example",
            emitted_at="2026-11-04T12:00:00Z",
            content="I will install it.",
        )
    )
    source = handler.store.captures()[0].utterance.source
    assert "\x00" not in source
    assert "I will install" not in source
