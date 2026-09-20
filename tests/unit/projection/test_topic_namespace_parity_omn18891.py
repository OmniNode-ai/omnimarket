# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The omnimarket namespace transform matches the canonical one (OMN-18891).

``omnimarket.topic_namespace`` exists because the projection-api process must
never load a database driver, and every ``omnibase_infra`` import pulls
``asyncpg`` through that package's own ``__init__`` (OMN-15800 AC6, pinned by
the subprocess gate in ``tests/integration/test_projection_bus_seam.py``). The
duplication is forced by that layering constraint, not chosen.

**A copy nobody compares is a copy that drifts.** This module is the
comparison. It imports both implementations and asserts they agree on the
variable name, on both directions over a corpus, on idempotence and on the
refusal. Importing the infra module here is fine: a test process is not the
projection-api process, and the gate that matters reads ``api_server``'s
import graph rather than this file's.

The corpus is deliberately shared between the two, so a change to one that the
other does not get fails here rather than on a lane six weeks later.
"""

from __future__ import annotations

import pytest
from omnibase_infra.topics import topic_namespace as canonical

from omnimarket import topic_namespace as local

pytestmark = [pytest.mark.unit]

#: Inputs chosen to cover each branch both transforms carry: a plain canonical
#: name, an already-prefixed name (idempotence), a near-miss that must NOT be
#: stripped, a tenant-prefixed name, and a name that is not ONEX-shaped at all.
_TOPIC_CORPUS: tuple[str, ...] = (
    "onex.evt.platform.node-registration.v1",
    "onex.snapshot.projection.live-events.v1",
    "prepr1.onex.evt.platform.node-registration.v1",
    "prepr10.onex.evt.platform.node-registration.v1",
    "tenant-acme.onex.evt.omnibase-infra.delegation-completed.v1",
    "node.registration.v1",
)

_NAMESPACE_VALUES: tuple[str, ...] = ("", "   ", "prepr1", "prepr2.", "slot-a")

_MALFORMED: tuple[str, ...] = ("Prepr1", "pre pr", "1slot", "slot!", "-slot", "a" * 40)


def test_the_environment_variable_name_is_identical() -> None:
    """One variable, read by both. Two names would be half an isolation."""
    assert local.TOPIC_NAMESPACE_ENV_VAR == canonical.TOPIC_NAMESPACE_ENV_VAR


@pytest.mark.parametrize("value", _NAMESPACE_VALUES)
def test_resolve_agrees(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(local.TOPIC_NAMESPACE_ENV_VAR, value)
    assert local.resolve_topic_namespace() == canonical.resolve_topic_namespace()


def test_resolve_agrees_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(local.TOPIC_NAMESPACE_ENV_VAR, raising=False)
    assert local.resolve_topic_namespace() == canonical.resolve_topic_namespace() == ""


@pytest.mark.parametrize("value", _NAMESPACE_VALUES)
@pytest.mark.parametrize("topic", _TOPIC_CORPUS)
def test_both_directions_agree(
    topic: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(local.TOPIC_NAMESPACE_ENV_VAR, value)
    assert local.apply_topic_namespace(topic) == canonical.apply_topic_namespace(topic)
    assert local.strip_topic_namespace(topic) == canonical.strip_topic_namespace(topic)


@pytest.mark.parametrize("value", _NAMESPACE_VALUES)
def test_the_list_form_agrees(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(local.TOPIC_NAMESPACE_ENV_VAR, value)
    assert local.apply_topic_namespace_all(_TOPIC_CORPUS) == (
        canonical.apply_topic_namespace_all(_TOPIC_CORPUS)
    )


@pytest.mark.parametrize("bad", _MALFORMED)
def test_both_refuse_the_same_malformed_tokens(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing closed in one copy and coercing in the other is the worst case."""
    monkeypatch.setenv(local.TOPIC_NAMESPACE_ENV_VAR, bad)
    with pytest.raises(local.TopicNamespaceError):
        local.resolve_topic_namespace()
    with pytest.raises(canonical.TopicNamespaceError):
        canonical.resolve_topic_namespace()


def test_the_corpus_actually_exercises_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the agreement above must not be agreement on nothing.

    Every assertion in this module would pass if both transforms were the
    identity. This asserts the corpus contains at least one input each
    direction genuinely changes, so a pair of no-op implementations cannot
    pass as a matching pair.
    """
    monkeypatch.setenv(local.TOPIC_NAMESPACE_ENV_VAR, "prepr1")
    applied = [local.apply_topic_namespace(t) for t in _TOPIC_CORPUS]
    stripped = [local.strip_topic_namespace(t) for t in _TOPIC_CORPUS]
    assert any(a != t for a, t in zip(applied, _TOPIC_CORPUS, strict=True))
    assert any(s != t for s, t in zip(stripped, _TOPIC_CORPUS, strict=True))
