# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build a golden-chain input from the PUBLISHER's contract (OMN-18013).

THE DEFECT CLASS THIS CLOSES
----------------------------
A golden-chain test that hand-types its own ``event_type`` proves nothing about the
bus. The runtime never stamps the full topic string as an event type: the auto-wiring
consume boundary stamps the ALIAS ``<producer>.<event-name>``
(``derive_event_type_alias_for_topic``), and the dispatcher index happens to accept
BOTH the literal topic and that alias as keys. So a test that passes
``event_type=<the full topic string>`` is green on a shape the bus does not carry, and
stays green when the real alias stops resolving. (The topics themselves are deliberately
not spelled in this module: it is imported by tests, and a topic literal here would be
the one place the whole rule is about that nothing checks.)

Measured 2026-09-06: 108 such sites across 44 files in four repos (55 in omnimarket).
Live control the same day: ``onex.evt.omnimarket.delegate-skill-completed.v1`` offset 221
carried ``event_type='omnimarket.delegate-skill-completed'`` on the wire.

THE RULE
--------
A test names the TOPIC -- the thing the contracts actually declare -- and this module
derives the event type the same way the runtime does, after proving some contract in
the corpus declares a PUBLISHER for that topic. A topic nothing publishes raises
:class:`NoDeclaredPublisherError`, so a test cannot be written against an edge the
graph does not have::

    from omnimarket.testing.publisher_contract_fixture import publisher_event_type

    envelope = ModelEventEnvelope(
        event_type=publisher_event_type(<the topic the publisher declares>),
        ...
    )

There is no ``allow_missing`` switch and no exemption list. If a topic has no declared
publisher, the contract graph is wrong and the fix is in the contract, not here.
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path

from omnibase_infra.event_bus.topic_constants import derive_event_type_alias_for_topic

from omnimarket.validators.contract_topic_graph import parse_contract

# Packages whose contract.yaml files are read to resolve a topic's publisher. These are
# resolved from the INSTALLED distribution (they are all pip/uv dependencies of
# omnimarket), never from a checkout path -- a test fixture must not depend on an env
# var pointing at a sibling clone. omnimarket itself is resolved from this file.
_PUBLISHER_PACKAGES: tuple[str, ...] = (
    "omnibase_infra",
    "omnibase_core",
    "omnimemory",
)


class NoDeclaredPublisherError(AssertionError):
    """No contract in the corpus declares a publisher for the topic."""

    def __init__(self, topic: str, scanned: int) -> None:
        super().__init__(
            f"No contract declares a publisher for {topic!r} "
            f"({scanned} contracts scanned across omnimarket + "
            f"{', '.join(_PUBLISHER_PACKAGES)}).\n"
            f"A golden chain cannot be written against an edge the contract graph does "
            f"not have: the test would be green on a message the bus can never carry.\n"
            f"Fix the CONTRACT -- add the topic to the producing node's "
            f"event_bus.publish_topics (or runtime_dispatch.terminal_events), or point "
            f"the test at the topic that is actually published."
        )


def _package_root(package: str) -> Path | None:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


@lru_cache(maxsize=1)
def _publisher_index() -> tuple[dict[str, tuple[str, ...]], int]:
    """Map every declared topic to the contract names that publish it.

    Uses the REAL parser (:func:`contract_topic_graph.parse_contract`), so the fixture
    reads the same dozen declaration shapes the graph oracle reads and cannot drift
    from it.
    """
    roots: dict[str, Path] = {}
    own = _package_root("omnimarket")
    if own is not None:
        roots["omnimarket"] = own
    for package in _PUBLISHER_PACKAGES:
        root = _package_root(package)
        if root is not None:
            roots[package] = root

    producers: dict[str, list[str]] = {}
    scanned = 0
    for package, root in roots.items():
        for path in sorted(root.rglob("contract.yaml")):
            try:
                node = parse_contract(path, package)
            except RuntimeError:
                continue
            if node is None:
                continue
            scanned += 1
            for topic in node.publish_topics:
                producers.setdefault(topic, []).append(node.name)
            # A node's runtime_dispatch.command_topic IS a declared producer: the CLI
            # dispatcher is what publishes to it. contract_topic_graph.is_reachable
            # treats it the same way, so the fixture and the graph oracle agree on what
            # counts as "something can put a message here".
            if node.command_topic is not None:
                producers.setdefault(node.command_topic, []).append(
                    f"{node.name} (runtime_dispatch.command_topic — CLI dispatcher)"
                )
    return {t: tuple(sorted(set(n))) for t, n in producers.items()}, scanned


def declared_publishers(topic: str) -> tuple[str, ...]:
    """Contract names that declare ``topic`` as a publish topic. Never raises."""
    index, _ = _publisher_index()
    return index.get(topic, ())


def publisher_event_type(topic: str) -> str:
    """The ``event_type`` the runtime stamps for ``topic``, proven publishable.

    Raises:
        NoDeclaredPublisherError: no contract in the corpus publishes ``topic``.
    """
    index, scanned = _publisher_index()
    if topic not in index:
        raise NoDeclaredPublisherError(topic, scanned)
    # The upstream helper is untyped at this pin; bind the contract here rather than
    # letting `Any` leak into every caller's assertion, where a non-str would only
    # surface as a confusing comparison failure inside a golden chain.
    alias: str = derive_event_type_alias_for_topic(topic)
    return alias
