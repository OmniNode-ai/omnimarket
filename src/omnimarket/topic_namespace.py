# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment topic namespace, omnimarket side (OMN-18891).

Semantically identical to ``omnibase_infra.topics.topic_namespace`` and
deliberately NOT importing it. The reason is a layering constraint, not
convenience, and it is narrow enough to state exactly:

**The projection-api process must never load a database driver.** OMN-15800
AC6 pins that with a subprocess gate
(``tests/integration/test_projection_bus_seam.py``) which imports
``omnimarket.projection.api_server`` in a fresh interpreter and asserts
``asyncpg`` is absent from ``sys.modules``. ``api_server`` imports both
``projection.runner`` and ``projection.snapshot_cache``, and **any**
``omnibase_infra`` import pulls ``asyncpg`` through that package's own
``__init__``. A single top-level ``from omnibase_infra.topics.topic_namespace
import ...`` in either module therefore fails that gate, which is how this
module came to exist: the first revision of OMN-18891's omnimarket slice did
exactly that and the gate caught it.

A lazy import inside the constructor would pass the gate and still be wrong.
The gate reads the import graph, but the rule is about the deployed process,
and ``SnapshotCache`` is constructed inside the projection-api process. Making
the violation later rather than absent is not a fix.

**This duplication is pinned, not trusted.** ``tests/unit/projection/
test_topic_namespace_parity_omn18891.py`` asserts this module and the
canonical one agree on the variable name and on a corpus of inputs covering
both directions, the idempotence property and the refusal. The test imports
both, which is fine: a test process is not the projection-api process.

**This is the second copy of the transform and there should eventually be
none.** The gateway API carries a third for an unrelated reason (it ships no
shared wheel at all). The real home is ``omnibase_compat``, which exists for
exactly this — cross-repo primitives with zero upstream runtime deps — and
which every one of the three can import without pulling a driver. Tracked as
a follow-up; until then the parity test is what keeps the copies honest.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

__all__ = [
    "TOPIC_NAMESPACE_ENV_VAR",
    "TopicNamespaceError",
    "apply_topic_namespace",
    "apply_topic_namespace_all",
    "resolve_topic_namespace",
    "strip_topic_namespace",
]

#: MUST stay byte-identical to the canonical module's constant. A slot sets
#: one value and every process reads it; two names would be a slot that is
#: half isolated. Pinned by the parity test.
TOPIC_NAMESPACE_ENV_VAR = "KAFKA_TOPIC_NAMESPACE"

#: A namespace token must be a safe single topic segment: lowercase, starting
#: with a letter, no dots. The dot separator is added here so a prefix
#: boundary is unambiguous and ``prepr1`` cannot match ``prepr10``.
_NAMESPACE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

#: Memoised on the RAW environment value. The strip direction runs once per
#: consumed record on the projection path, and re-validating a value that
#: changes only at process start would put a regex match on every message.
#: Keyed on the raw string so a test that patches the variable still sees the
#: new value.
_NORMALISED: dict[str, str] = {}


class TopicNamespaceError(ValueError):
    """Raised when the configured namespace token is not a usable segment.

    Fails closed rather than coercing, because a typo would otherwise route a
    process to a namespace nobody is watching, which reads exactly like
    isolation working.
    """


def resolve_topic_namespace(env: Mapping[str, str] | None = None) -> str:
    """Return the physical namespace prefix, including its trailing dot.

    Returns ``""`` when the variable is absent, empty or whitespace-only,
    which is every lane running today.
    """
    source = os.environ if env is None else env
    raw = source.get(TOPIC_NAMESPACE_ENV_VAR, "")
    cached = _NORMALISED.get(raw)
    if cached is not None:
        return cached
    token = raw.strip()
    if not token:
        _NORMALISED[raw] = ""
        return ""
    if token.endswith("."):
        token = token[:-1]
    if not _NAMESPACE_TOKEN_RE.match(token):
        raise TopicNamespaceError(
            f"Invalid {TOPIC_NAMESPACE_ENV_VAR} value {raw!r}: a topic namespace "
            "must be a single lowercase segment matching "
            f"{_NAMESPACE_TOKEN_RE.pattern!r} (an optional trailing dot is "
            "accepted and normalised away)."
        )
    normalised = f"{token}."
    _NORMALISED[raw] = normalised
    return normalised


def apply_topic_namespace(
    topic: str,
    *,
    namespace: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Map a topic name to its physical name on this broker.

    Idempotent, so a seam cannot double-prefix by being called twice on one
    path.
    """
    prefix = resolve_topic_namespace(env) if namespace is None else namespace
    if not prefix or topic.startswith(prefix):
        return topic
    return f"{prefix}{topic}"


def apply_topic_namespace_all(
    topics: Iterable[str],
    *,
    namespace: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Map several topic names to physical names, order preserved."""
    prefix = resolve_topic_namespace(env) if namespace is None else namespace
    return [apply_topic_namespace(t, namespace=prefix) for t in topics]


def strip_topic_namespace(
    topic: str,
    *,
    namespace: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Map a physical topic name back towards its canonical name.

    What every consume boundary calls BEFORE it compares or indexes by topic
    name. Deliberately tolerant of a name carrying no prefix: during a
    roll-out a namespaced consumer may be handed either form, and a strip that
    refused the bare one would turn a benign case into an exception on the
    message path.
    """
    prefix = resolve_topic_namespace(env) if namespace is None else namespace
    if prefix and topic.startswith(prefix):
        return topic[len(prefix) :]
    return topic
