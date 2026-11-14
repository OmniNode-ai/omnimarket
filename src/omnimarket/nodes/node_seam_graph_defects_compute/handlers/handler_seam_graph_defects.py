# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derives the nine seam defect classes over one scan's graph (OMN-19998).

The node is the verification half of the seam graph: the facts loader reads
the pinned checkouts and the declared contracts, this module only reports the
defects visible at the seams between the nodes. It carries no effect and it
never reports clean for work it did not do: a scan that read no contract
refuses instead of returning an empty report.
"""

from __future__ import annotations

import re

from omnimarket.handlers.seam_graph_facts_loader import load_seam_graph_facts
from omnimarket.models.model_seam_graph_defects import (
    EnumSeamDefectClass,
    ModelSeamDefect,
    ModelSeamDefectGroup,
    ModelSeamGraphCounts,
    ModelSeamGraphDefectReport,
    ModelSeamGraphFacts,
    ModelSeamGraphRequest,
    ModelSeamGraphRule,
    ModelSeamImportFact,
    ModelSeamNodeFact,
)


def _node_id(node: ModelSeamNodeFact) -> str:
    """Return the stable ``<repo>:<name>`` identifier of a node fact."""
    return f"{node.repo}:{node.name}"


def _is_command(topic: str, command_pattern: re.Pattern[str]) -> bool:
    """Return whether a topic literal is a command topic under the rule."""
    return command_pattern.fullmatch(topic) is not None


def _first_ids(sorted_ids: list[str]) -> str:
    """Render the first three entries of an already sorted id list."""
    return ", ".join(sorted_ids[:3])


def _first_hits(hits: tuple[str, ...]) -> str:
    """Render the first three hit entries joined as one detail line."""
    return "; ".join(hits[:3])


def derive_seam_defects(
    facts: ModelSeamGraphFacts,
    rule: ModelSeamGraphRule,
) -> ModelSeamGraphDefectReport:
    """Derive the nine defect groups from the facts of one seam graph scan.

    Every declared defect class appears in the report, in enum declaration
    order, even at count zero; each group carries its total count and the
    first ``rule.first_n`` defects sorted by ``(repo, subject)``. Deterministic
    in any input order: no clock, no randomness, every output list sorted.

    Raises:
        ValueError: when the scan read no contract. A verification node never
            reports clean for work it did not do.
    """
    if not facts.nodes:
        raise ValueError("vacuous seam graph: zero contracts read")

    try:
        command_pattern = re.compile(rule.command_topic_pattern)
    except re.error as error:
        raise ValueError(
            f"command_topic_pattern {rule.command_topic_pattern!r} is not a valid "
            f"regex: {error}"
        ) from error

    nodes = facts.nodes
    nodes_by_id = {_node_id(node): node for node in nodes}

    publishers: dict[str, set[str]] = {}
    subscribers: dict[str, set[str]] = {}
    ext_produced: set[str] = set()
    ext_consumed: set[str] = set()
    for node in nodes:
        node_id = _node_id(node)
        for topic in node.publish_topics:
            publishers.setdefault(topic, set()).add(node_id)
        for topic in node.subscribe_topics:
            subscribers.setdefault(topic, set()).add(node_id)
        ext_produced.update(node.external_produced_topics)
        ext_consumed.update(node.external_consumed_topics)

    defects: dict[EnumSeamDefectClass, list[ModelSeamDefect]] = {
        defect_class: [] for defect_class in EnumSeamDefectClass
    }

    # 1. Topics subscribed with no publisher. A command topic is its own
    # demand, and an externally produced topic is fed from outside the node
    # graph, so neither is a defect when no node publishes it.
    for topic in sorted(subscribers):
        if _is_command(topic, command_pattern):
            continue
        if topic in publishers or topic in ext_produced:
            continue
        subscriber_ids = sorted(subscribers[topic])
        defects[EnumSeamDefectClass.TOPIC_SUBSCRIBED_NO_PUBLISHER].append(
            ModelSeamDefect(
                defect_class=EnumSeamDefectClass.TOPIC_SUBSCRIBED_NO_PUBLISHER,
                subject=topic,
                repo=nodes_by_id[subscriber_ids[0]].repo,
                detail=f"subscribed by {len(subscriber_ids)}: "
                f"{_first_ids(subscriber_ids)}",
            )
        )

    # 2. Topics published with no subscriber; the same field style.
    for topic in sorted(publishers):
        if _is_command(topic, command_pattern):
            continue
        if topic in subscribers or topic in ext_consumed:
            continue
        publisher_ids = sorted(publishers[topic])
        defects[EnumSeamDefectClass.TOPIC_PUBLISHED_NO_SUBSCRIBER].append(
            ModelSeamDefect(
                defect_class=EnumSeamDefectClass.TOPIC_PUBLISHED_NO_SUBSCRIBER,
                subject=topic,
                repo=nodes_by_id[publisher_ids[0]].repo,
                detail=f"published by {len(publisher_ids)}: "
                f"{_first_ids(publisher_ids)}",
            )
        )

    # 3. Nodes nothing calls: no reference outside their own directory, no
    # publisher of their subscribed topics, nothing external feeding them.
    for node in sorted(nodes, key=_node_id):
        if node.referenced_by:
            continue
        node_id = _node_id(node)
        called_by_another = any(
            publishers.get(topic, set()) - {node_id} for topic in node.subscribe_topics
        )
        fed_from_outside = any(topic in ext_produced for topic in node.subscribe_topics)
        if called_by_another or fed_from_outside:
            continue
        defects[EnumSeamDefectClass.NODE_NO_CALLER].append(
            ModelSeamDefect(
                defect_class=EnumSeamDefectClass.NODE_NO_CALLER,
                subject=node.name,
                repo=node.repo,
                detail="no reference outside its own directory and no publisher "
                "of its subscribed topics",
            )
        )

    # 4 and 5. Overlay coverage: a node that needs an overlay either has no
    # row at all, or a row missing required keys.
    for node in sorted(nodes, key=_node_id):
        if not node.needs_overlay:
            continue
        if not node.overlay_row_present:
            defects[EnumSeamDefectClass.NODE_OVERLAY_ROW_MISSING].append(
                ModelSeamDefect(
                    defect_class=EnumSeamDefectClass.NODE_OVERLAY_ROW_MISSING,
                    subject=node.name,
                    repo=node.repo,
                    detail=f"no overlay row under {rule.overlay_repo} matching "
                    f"{rule.overlay_row_glob}",
                )
            )
            continue
        missing_keys = sorted(
            set(node.overlay_required_keys) - set(node.overlay_row_keys)
        )
        if missing_keys:
            defects[EnumSeamDefectClass.NODE_OVERLAY_KEYS_MISSING].append(
                ModelSeamDefect(
                    defect_class=EnumSeamDefectClass.NODE_OVERLAY_KEYS_MISSING,
                    subject=node.name,
                    repo=node.repo,
                    detail=f"missing overlay keys: {', '.join(missing_keys)}",
                )
            )

    # 6. Cross-repo imports no seam test covers, collapsed to one defect per
    # (importer_repo, module).
    uncovered_imports: dict[tuple[str, str], list[ModelSeamImportFact]] = {}
    for import_fact in facts.imports:
        if import_fact.has_seam_test:
            continue
        uncovered_imports.setdefault(
            (import_fact.importer_repo, import_fact.module), []
        ).append(import_fact)
    for (importer_repo, module), group in sorted(uncovered_imports.items()):
        ordered = sorted(group, key=lambda fact: fact.importer_path)
        defects[EnumSeamDefectClass.CROSS_REPO_IMPORT_NO_SEAM_TEST].append(
            ModelSeamDefect(
                defect_class=EnumSeamDefectClass.CROSS_REPO_IMPORT_NO_SEAM_TEST,
                subject=module,
                repo=importer_repo,
                detail=f"imported by {len(ordered)} file(s) of {importer_repo}, "
                f"owned by {ordered[0].owner_repo}; first: {ordered[0].importer_path}",
            )
        )

    # 7 and 8. Handler hygiene: synchronous I/O inside a handle and file-backed
    # ledger reads.
    for node in sorted(nodes, key=_node_id):
        if node.sync_io_hits:
            defects[EnumSeamDefectClass.SYNC_IO_HANDLER].append(
                ModelSeamDefect(
                    defect_class=EnumSeamDefectClass.SYNC_IO_HANDLER,
                    subject=node.name,
                    repo=node.repo,
                    detail=_first_hits(node.sync_io_hits),
                )
            )
        if node.ledger_reader_hits:
            defects[EnumSeamDefectClass.FILE_LEDGER_READER].append(
                ModelSeamDefect(
                    defect_class=EnumSeamDefectClass.FILE_LEDGER_READER,
                    subject=node.name,
                    repo=node.repo,
                    detail=_first_hits(node.ledger_reader_hits),
                )
            )

    # 9. A replay-marked node publishing a topic read by at least the consumer
    # budget of other nodes; one defect per node, first qualifying topic in
    # sorted order.
    for node in sorted(nodes, key=_node_id):
        if not any(marker in node.name for marker in rule.replay_node_markers):
            continue
        node_id = _node_id(node)
        for topic in sorted(node.publish_topics):
            readers = subscribers.get(topic, set()) - {node_id}
            if len(readers) < rule.shared_topic_min_consumers:
                continue
            defects[EnumSeamDefectClass.SHARED_TOPIC_REPLAY].append(
                ModelSeamDefect(
                    defect_class=EnumSeamDefectClass.SHARED_TOPIC_REPLAY,
                    subject=node.name,
                    repo=node.repo,
                    detail=f"publishes {topic} read by {len(readers)} nodes",
                )
            )
            break

    counts = ModelSeamGraphCounts(
        nodes=len(nodes),
        topics=len(set(publishers) | set(subscribers)),
        publish_edges=sum(len(node.publish_topics) for node in nodes),
        subscribe_edges=sum(len(node.subscribe_topics) for node in nodes),
        overlay_edges=sum(1 for node in nodes if node.overlay_row_present),
        caller_edges=sum(len(node.referenced_by) for node in nodes),
        import_edges=len(facts.imports),
    )

    groups = tuple(
        ModelSeamDefectGroup(
            defect_class=defect_class,
            count=len(class_defects),
            first=tuple(
                sorted(class_defects, key=lambda d: (d.repo, d.subject, d.detail))[
                    : rule.first_n
                ]
            ),
        )
        for defect_class, class_defects in defects.items()
    )

    return ModelSeamGraphDefectReport(
        counts=counts,
        groups=groups,
        contracts_read=facts.contracts_read,
        contracts_unreadable=facts.contracts_unreadable,
    )


class HandlerSeamGraphDefects:
    """Handles a seam graph request and returns the defect report."""

    def handle(self, request: ModelSeamGraphRequest) -> ModelSeamGraphDefectReport:
        """Loads the facts of the pinned checkouts and derives the defects."""
        return derive_seam_defects(load_seam_graph_facts(request), request.rule)
