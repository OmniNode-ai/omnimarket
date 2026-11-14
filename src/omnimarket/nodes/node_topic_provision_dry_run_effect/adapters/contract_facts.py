# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read contract.yaml files under the given roots and build ModelTopicProvisionContract tuples."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml
from omnibase_infra.tools.contract_topic_extractor import read_projection_api_topics

from omnimarket.models.topic_provision.model_topic_provision_contract import (
    ModelTopicProvisionContract,
)


def _extract_topics(data: dict[str, object]) -> list[str]:
    """Extract all topic names declared by a contract mapping."""
    topics: set[str] = set()

    def _add(value: object) -> None:
        if isinstance(value, str) and value:
            topics.add(value)
        elif isinstance(value, dict):
            for key in ("topic", "success_topic", "failure_topic"):
                v = value.get(key)
                if isinstance(v, str) and v:
                    topics.add(v)

    event_bus = data.get("event_bus")
    if isinstance(event_bus, dict):
        for key in ("subscribe_topics", "publish_topics", "dlq_topics"):
            entries = event_bus.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    _add(entry)

    for key in ("consumed_events", "published_events"):
        entries = data.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    _add(entry.get("topic"))

    runtime_dispatch = data.get("runtime_dispatch")
    if isinstance(runtime_dispatch, dict):
        _add(runtime_dispatch.get("command_topic"))
        terminal = runtime_dispatch.get("terminal_events")
        if isinstance(terminal, dict):
            for v in terminal.values():
                _add(v)

    return sorted(topics)


def read_contract_facts(
    roots: Sequence[Path],
) -> tuple[ModelTopicProvisionContract, ...]:
    """Read every contract.yaml under each root, in sorted path order.

    Skips files that are not a mapping. contract_name is the top-level
    ``contract_name``, else ``name``, else the parent directory name.
    runtime_profiles is the union of top-level and descriptor.runtime_profiles.
    """
    results: list[ModelTopicProvisionContract] = []

    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("contract.yaml")):
            if not path.is_file():
                continue
            try:
                with path.open(encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(data, dict):
                continue

            name = data.get("contract_name")
            if not isinstance(name, str) or not name:
                name = data.get("name")
            if not isinstance(name, str) or not name:
                name = path.parent.name

            profiles: set[str] = set()
            top_profiles = data.get("runtime_profiles")
            if isinstance(top_profiles, list):
                for p in top_profiles:
                    if isinstance(p, str) and p:
                        profiles.add(p)
            descriptor = data.get("descriptor")
            if isinstance(descriptor, dict):
                desc_profiles = descriptor.get("runtime_profiles")
                if isinstance(desc_profiles, list):
                    for p in desc_profiles:
                        if isinstance(p, str) and p:
                            profiles.add(p)

            # The served projection_api topics come from the platform's own reader, the
            # one the boot-side per-contract provisioner uses, so the two cannot disagree.
            topics = sorted({*_extract_topics(data), *read_projection_api_topics(path)})

            results.append(
                ModelTopicProvisionContract(
                    contract_name=name,
                    runtime_profiles=tuple(sorted(profiles)),
                    topics=tuple(topics),
                )
            )

    return tuple(results)
