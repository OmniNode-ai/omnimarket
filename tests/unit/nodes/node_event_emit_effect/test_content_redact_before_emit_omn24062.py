# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The registry flip, proven end to end: content.captured is redacted before
emit (OMN-24062).

OMN-24062 changed ``content.captured``'s fan-out rule from ``passthrough`` to
``redact_capture``, so credential-shaped spans of ``content`` and ``command``
are replaced in place before the record is published to
onex.cmd.omniintelligence.content-captured.v1. These tests resolve the route
the way the emit path does -- through ``resolve_event_type`` and the transform
the resolved target names, bound to its topic exactly as the daemon loader
binds it -- and prove on every planted fixture from
``test_content_capture_omn19550.py`` that the secret does not reach the
published record while the text around it does.

The control at the bottom applies the OLD transform, ``passthrough``, to the
same record: the planted token crosses unchanged. That is the shape of the
change before the registry flip, and it shows the no-secret assertion the
routed route above makes is one the old route FAILED.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from typing import Any

import pytest

from omnimarket.nodes.node_emit_daemon.event_registry import (
    TOPIC_SCOPED_TRANSFORM_REGISTRY,
    TRANSFORM_REGISTRY,
)
from omnimarket.nodes.node_event_emit_effect.errors import UngovernedTopicError
from omnimarket.nodes.node_event_emit_effect.redaction import (
    load_contract,
    plan_content_chunks,
    redact_capture,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
)
from tests.unit.nodes.node_event_emit_effect import (
    test_content_capture_omn19550 as capture_19550,
)

CONTENT_EVENT = "content.captured"


def _routed_transform() -> tuple[Callable[[dict[str, Any]], dict[str, Any]], str]:
    """The fan-out target's transform, bound to its topic as the daemon does."""
    (target,) = resolve_event_type(CONTENT_EVENT)
    assert target.transform_name == "redact_capture"
    transform = TOPIC_SCOPED_TRANSFORM_REGISTRY[target.transform_name]
    return partial(transform, topic=target.topic), target.topic


# ---------------------------------------------------------------------------
# (a) golden chain: every planted secret is scrubbed on the resolved route
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(capture_19550.PLANTED))
def test_the_routed_transform_publishes_no_planted_secret(name: str) -> None:
    before, secret, after = capture_19550.PLANTED[name]
    transform, _topic = _routed_transform()
    published = transform(capture_19550._content_record(before + secret + after))

    assert capture_19550._F not in json.dumps(published), (
        f"{name}: the planted value reached the published record"
    )
    content = published["content"]
    assert isinstance(content, str)
    assert "[REDACTED:" in content
    # The text around the span is intact: scrub in place, not a hash of the
    # whole field. Same context assertions as the scrub tests in
    # test_content_capture_omn19550.py, on the ROUTED transform.
    assert content.startswith(before.rstrip("'").rstrip()[:4])
    assert after.strip()[-3:] in content


@pytest.mark.unit
def test_the_routed_transform_resolves_from_the_registry_entry() -> None:
    """The proof rides the route the emit path resolves, not a direct call."""
    transform, topic = _routed_transform()
    assert topic == capture_19550.CONTENT_TOPIC
    assert transform(capture_19550._content_record("plain"))["content"] == "plain"


# ---------------------------------------------------------------------------
# (b) the size cap: nothing within the item cap is cut by the transform
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_content_item_within_the_cap_crosses_the_transform_uncut() -> None:
    """A 70,000-character plain item is not cut, because the producer chunks it.

    The contract bounds one content ITEM at ``content_policy.max_content_chars``
    -- enforced by the producer through ``plan_content_chunks``, which splits at
    ``chunk_chars`` -- and one RECORD at ``chunk_chars``, the transform's own
    fail-safe for a producer that did not chunk. 70,000 characters are within
    the item cap, so the producer publishes all of them as chunks within the
    record bound, and every one of the 70,000 must survive the transform.
    """
    policy = load_contract().topics[capture_19550.CONTENT_TOPIC].content_policy
    assert policy is not None
    content = "a" * 70_000
    assert len(content) <= policy.max_content_chars

    plan = plan_content_chunks(content, capture_19550.CONTENT_TOPIC)
    assert plan.truncated is False  # within the item cap: nothing is dropped
    assert "".join(plan.chunks) == content
    assert all(len(chunk) <= policy.chunk_chars for chunk in plan.chunks)

    transform, _topic = _routed_transform()
    crossed: list[str] = []
    for index, chunk in enumerate(plan.chunks):
        record = capture_19550._content_record(
            chunk, chunk_index=index, chunk_count=len(plan.chunks), truncated=False
        )
        published = transform(record)
        assert published["content"] == chunk, (
            f"chunk {index}: the transform cut a chunk the producer sized"
        )
        assert published["truncated"] is False
        crossed.append(published["content"])
    # All 70,000 characters crossed the transform uncut.
    assert "".join(crossed) == content


# ---------------------------------------------------------------------------
# (c) the error chain: an ungoverned topic is a refusal, not a default
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_redact_capture_refuses_an_ungoverned_topic() -> None:
    ungoverned = "onex.cmd.omniintelligence.tool-content.v1"
    assert ungoverned not in load_contract().topics
    with pytest.raises(UngovernedTopicError, match="redact_capture") as excinfo:
        redact_capture(capture_19550._content_record("hello"), ungoverned)
    assert excinfo.value.topic == ungoverned
    assert capture_19550.CONTENT_TOPIC in excinfo.value.governed


# ---------------------------------------------------------------------------
# (d) failing control: the OLD transform published the planted token
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_old_passthrough_publishes_the_planted_token() -> None:
    """The shape of the change before the registry flip.

    ``passthrough`` is still what TRANSFORM_REGISTRY offers a fan-out rule that
    wants no transform, and it is what content.captured named before OMN-24062.
    Applied to the same record the routed route scrubs above, the planted token
    crosses unchanged -- so the no-FAKE assertion in (a) is one that the old
    route FAILED, not an assertion that could never fail.
    """
    before, secret, after = capture_19550.PLANTED["github_token"]
    record = capture_19550._content_record(before + secret + after)
    published = TRANSFORM_REGISTRY["passthrough"](record)

    assert published["content"] == before + secret + after
    assert capture_19550._F in published["content"]
    assert secret in published["content"]
