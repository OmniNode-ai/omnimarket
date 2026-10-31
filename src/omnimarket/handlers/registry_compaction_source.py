# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compacted-source reading for the registry projection reconcile gate.

The gate (``scripts/ci/check_registry_projection_reconcile.py``, OMN-17446)
imports these from here: which cleanup policy this repo's contracts declare
for a topic, whether that policy compacts without deleting, and which node
directories import a shared projection module (OMN-21217).
"""

from __future__ import annotations

import ast
import functools
import pathlib


class GateInputError(RuntimeError):
    """The gate could not read one of its own inputs.

    Raised rather than swallowed. A gate that cannot read its own exemption
    list has not passed -- it has not run, and the two must not look alike
    (the same posture the public-repo hygiene gate takes when its vocabulary
    is missing).
    """


# The contract sites the topic provisioner reads a topic's ``topic_config``
# from (omnibase_infra contract_topic_extractor, OMN-13238 and OMN-15832).
_EVENT_SECTION_KEYS = ("consumed_events", "published_events", "produced_events")
_EVENT_BUS_SECTION_KEYS = ("subscribe_topics", "publish_topics", "dlq_topics")


def _served_exposures(projection_api: object) -> list[dict[object, object]]:
    """The exposures the provisioner creates: ``expose: true`` on the section
    and ``bus_backed: true`` on the exposure, the same rule as the extractor."""
    if not isinstance(projection_api, dict) or projection_api.get("expose") is not True:
        return []
    exposures = projection_api.get("exposures")
    if exposures is None:
        items: list[object] = [projection_api]
    elif isinstance(exposures, list):
        items = list(exposures)
    else:
        items = []
    return [
        item
        for item in items
        if isinstance(item, dict) and item.get("bus_backed") is True
    ]


def _topic_declarations(
    contract: dict[str, object],
) -> list[tuple[object, dict[object, object]]]:
    """Every (topic name, item) of one contract that can carry a ``topic_config``.

    An event section item names its topic under ``topic`` or the old-style
    ``name``, and the provisioner applies the item's config to both; an
    ``event_bus`` item and a served exposure name it under ``topic`` only.
    """
    pairs: list[tuple[object, dict[object, object]]] = []
    for key in _EVENT_SECTION_KEYS:
        section = contract.get(key)
        if isinstance(section, list):
            for item in section:
                if isinstance(item, dict):
                    pairs.extend((item.get(k), item) for k in ("topic", "name"))
    event_bus = contract.get("event_bus")
    if isinstance(event_bus, dict):
        for key in _EVENT_BUS_SECTION_KEYS:
            section = event_bus.get(key)
            if isinstance(section, list):
                pairs.extend(
                    (item.get("topic"), item)
                    for item in section
                    if isinstance(item, dict)
                )
    pairs.extend(
        (item.get("topic"), item)
        for item in _served_exposures(contract.get("projection_api"))
    )
    return pairs


def _declared_cleanup_policies(
    contracts: list[tuple[str, dict[str, object]]],
) -> dict[str, str]:
    """topic -> the ``cleanup.policy`` this repo's contracts declare for it.

    Two contracts that declare different policies for one topic leave the
    gate unable to say which one the provisioner applied, so that is an input
    error, not a pass for whichever was read first.
    """
    declared: dict[str, tuple[str, str]] = {}
    for node, contract in contracts:
        for topic, item in _topic_declarations(contract):
            topic_config = item.get("topic_config")
            if not isinstance(topic, str) or not isinstance(topic_config, dict):
                continue
            kafka_config = topic_config.get("kafka_config")
            if (
                not isinstance(kafka_config, dict)
                or "cleanup.policy" not in kafka_config
            ):
                continue
            policy = str(kafka_config["cleanup.policy"]).strip()
            earlier = declared.setdefault(topic, (policy, node))
            if earlier[0] != policy:
                raise GateInputError(
                    f"{topic} declares cleanup.policy {earlier[0]!r} in "
                    f"{earlier[1]} and {policy!r} in {node}; the gate cannot "
                    "tell which one the topic was created with."
                )
    return {topic: policy for topic, (policy, _) in declared.items()}


def _is_compacted(topic: str, declared: dict[str, str]) -> bool:
    """A topic is a compacted source only when this repo's contracts declare
    a cleanup policy for it that compacts and does not delete.

    The name proves nothing about how the topic was created: the taxonomy's
    snapshot default is ``compact,delete`` with a 7-day retention, and with
    ``compact,delete`` Kafka still discards old segments by retention, so the
    latest row of a key can age off and the relation could not be rebuilt.
    """
    policy = declared.get(topic)
    if policy is None:
        return False
    parts = {part.strip() for part in policy.split(",")}
    return "compact" in parts and "delete" not in parts


def _projection_imports(tree: ast.AST) -> set[str]:
    """Return the set of projection stems that *tree* imports.

    A stem is recorded when any of the following absolute import forms
    appears in the tree::

        import omnimarket.projection.<stem>
        from omnimarket.projection.<stem> import name
        from omnimarket.projection import <stem>

    Relative imports are ignored.  A dotted remainder such as
    ``omnimarket.projection.a.b`` yields the stem ``a.b``.
    """
    stems: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                prefix = "omnimarket.projection."
                if alias.name.startswith(prefix):
                    stems.add(alias.name[len(prefix) :])
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            if node.module == "omnimarket.projection":
                for alias in node.names:
                    stems.add(alias.name)
            elif node.module.startswith("omnimarket.projection."):
                stems.add(node.module[len("omnimarket.projection.") :])
    return stems


@functools.cache
def _node_projection_importers(
    repo_root: pathlib.Path,
) -> dict[str, frozenset[str]]:
    """Map each projection stem to the node directories that import it.

    Each ``*.py`` file under ``repo_root/src/omnimarket/nodes`` is parsed
    exactly once.  Tests that create files under a tmp repo root get a fresh
    cache entry because the root differs.  A long-lived process scanning the
    same root twice after editing files must call
    ``_node_projection_importers.cache_clear()``.
    """
    nodes_dir = repo_root / "src" / "omnimarket" / "nodes"
    index: dict[str, set[str]] = {}
    for py_path in nodes_dir.rglob("*.py"):
        try:
            tree = ast.parse(
                py_path.read_text(encoding="utf-8", errors="replace"),
            )
        except SyntaxError as exc:
            raise GateInputError(
                f"cannot parse node module {py_path}",
            ) from exc
        node_dir = py_path.relative_to(nodes_dir).parts[0]
        for stem in _projection_imports(tree):
            index.setdefault(stem, set()).add(node_dir)
    return {stem: frozenset(dirs) for stem, dirs in index.items()}
