# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven topic discovery for the OmniMarket domain.

Reads ``event_bus.subscribe_topics`` and ``event_bus.publish_topics`` from
omnimarket node ``contract.yaml`` files and returns the collected lists.

Design decisions:
    - Topics are declared in each node's contract.yaml (source of truth).
    - This module reads those contracts via ``importlib.resources``.
    - The module also provides ``canonical_topic_to_dispatch_alias`` to convert
      ONEX canonical topic naming (``.cmd.`` / ``.evt.``) to the dispatch engine
      format (``.commands.`` / ``.events.``).

Related:
    - OMN-7645: PluginMarket domain plugin
    - OMN-2213: Reference implementation in omnimemory
"""

from __future__ import annotations

import importlib.resources
import logging
from collections.abc import Callable

import yaml

logger = logging.getLogger(__name__)

# ============================================================================
# Node packages that declare event_bus topics
# ============================================================================
# All omnimarket nodes with subscribe_topics in their contract.yaml.
# This list is maintained manually; add new event-bus-enabled nodes here.

_OMNIMARKET_EVENT_BUS_NODE_PACKAGES: list[str] = [
    "omnimarket.nodes.node_merge_sweep",
    "omnimarket.nodes.node_dod_verify",
    "omnimarket.nodes.node_data_flow_sweep",
    "omnimarket.nodes.node_close_out",
    "omnimarket.nodes.node_compliance_sweep",
    "omnimarket.nodes.node_runtime_sweep",
    "omnimarket.nodes.node_build_loop",
    "omnimarket.nodes.node_projection_session_outcome",
    "omnimarket.nodes.node_projection_delegation",
    "omnimarket.nodes.node_projection_baselines",
    "omnimarket.nodes.node_projection_llm_cost",
    "omnimarket.nodes.node_projection_registration",
    "omnimarket.nodes.node_projection_savings",
    "omnimarket.nodes.node_coverage_sweep",
    "omnimarket.nodes.node_log_projection",
    "omnimarket.nodes.node_platform_readiness",
    "omnimarket.nodes.node_pr_polish",
    "omnimarket.nodes.node_ticket_pipeline",
    "omnimarket.nodes.node_aislop_sweep",
    "omnimarket.nodes.node_hostile_reviewer",
    "omnimarket.nodes.node_local_review",
    "omnimarket.nodes.node_golden_chain_sweep",
    "omnimarket.nodes.node_dashboard_sweep",
    "omnimarket.nodes.node_data_verification",
    "omnimarket.nodes.node_process_watchdog",
]


# ============================================================================
# Public API
# ============================================================================


def collect_subscribe_topics_from_contracts(
    *,
    node_packages: list[str] | None = None,
) -> list[str]:
    """Collect subscribe topics from omnimarket node contracts.

    Scans ``contract.yaml`` files from omnimarket nodes and extracts
    ``event_bus.subscribe_topics`` from each node that declares them.

    Args:
        node_packages: Override list of node packages to scan.

    Returns:
        Ordered list of subscribe topic strings.
    """
    packages = node_packages or _OMNIMARKET_EVENT_BUS_NODE_PACKAGES
    all_topics: list[str] = []

    for package in packages:
        topics = _safe_read_topics(_read_subscribe_topics, package)
        if topics is not None:
            all_topics.extend(topics)

    logger.debug(
        "Collected %d omnimarket subscribe topics from %d contracts",
        len(all_topics),
        len(packages),
    )

    return all_topics


def collect_publish_topics_for_dispatch(
    *,
    node_packages: list[str] | None = None,
) -> dict[str, str]:
    """Collect publish topics from contracts and map to dispatch engine keys.

    Args:
        node_packages: Override list of node packages to scan.

    Returns:
        Dict mapping dispatch key to first publish topic string.
    """
    packages = node_packages or _OMNIMARKET_EVENT_BUS_NODE_PACKAGES
    result: dict[str, str] = {}

    for package in packages:
        topics = _safe_read_topics(_read_publish_topics, package)
        if topics:
            key = _derive_dispatch_key(package)
            result[key] = topics[0]

    logger.debug(
        "Collected %d publish topics for dispatch engine: %s",
        len(result),
        result,
    )

    return result


def canonical_topic_to_dispatch_alias(topic: str) -> str:
    """Convert ONEX canonical topic naming to dispatch engine format.

    Args:
        topic: Canonical topic string.

    Returns:
        Dispatch-compatible topic string.
    """
    return topic.replace(".cmd.", ".commands.").replace(".evt.", ".events.")


# ============================================================================
# Internal helpers
# ============================================================================


def _safe_read_topics(
    reader: Callable[[str], list[str]],
    package: str,
) -> list[str] | None:
    """Call *reader* for *package*, handling common contract read errors."""
    try:
        return reader(package)
    except FileNotFoundError:
        logger.warning(
            "contract.yaml not found in package %s, skipping",
            package,
        )
        return None
    except ModuleNotFoundError:
        logger.warning(
            "Package %s is not installed/importable, skipping",
            package,
        )
        return None
    except yaml.YAMLError:
        logger.warning(
            "contract.yaml in package %s contains invalid YAML, skipping",
            package,
        )
        return None


def _derive_dispatch_key(package: str) -> str:
    """Derive a dispatch key from a fully-qualified node package path."""
    tail = package.rsplit(".", 1)[-1]
    for suffix in ("_effect", "_orchestrator", "_compute", "_reducer"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    if tail.startswith("node_"):
        tail = tail[len("node_") :]
    return tail


def _read_event_bus_topics(package: str, field: str) -> list[str]:
    """Read a topic list from a node package's ``event_bus`` contract section."""
    package_files = importlib.resources.files(package)
    contract_file = package_files.joinpath("contract.yaml")
    content = contract_file.read_text(encoding="utf-8")
    contract: object = yaml.safe_load(content)

    if not isinstance(contract, dict):
        return []

    event_bus: object = contract.get("event_bus", {})
    if not isinstance(event_bus, dict):
        return []

    topics_raw: object = event_bus.get(field, [])
    if not isinstance(topics_raw, list):
        return []

    return [t for t in topics_raw if isinstance(t, str)]


def _read_subscribe_topics(package: str) -> list[str]:
    """Read ``event_bus.subscribe_topics`` from a node package's contract."""
    return _read_event_bus_topics(package, "subscribe_topics")


def _read_publish_topics(package: str) -> list[str]:
    """Read ``event_bus.publish_topics`` from a node package's contract."""
    return _read_event_bus_topics(package, "publish_topics")


__all__ = [
    "canonical_topic_to_dispatch_alias",
    "collect_publish_topics_for_dispatch",
    "collect_subscribe_topics_from_contracts",
]
