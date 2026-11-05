# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the runtime's own discovery manifest and hash-match installed contracts."""

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from omnimarket.models.model_command_invocation import ModelCommandInvocation
from omnimarket.models.model_command_seam import (
    ModelCommandSeamExternalSender,
    ModelCommandSeamNode,
    ModelCommandSeamOverlay,
)


def _installed_binding(
    contract: dict[str, object], overlay: ModelCommandSeamOverlay
) -> tuple[str | None, ModelCommandInvocation | None, str | None, dict[str, str]]:
    package = str(contract["package_name"])
    root = overlay.package_roots.get(package)
    if root is None:
        return None, None, "package-source-unconfigured", {}
    directory = Path(str(contract["contract_path"])).parent.name
    # The remote manifest supplies an identifier, never a path to open locally.
    if (
        not directory.startswith("node_")
        or "/" in directory
        or directory in {".", ".."}
    ):
        return None, None, "contract-directory-invalid", {}
    path = Path(root) / "nodes" / directory / "contract.yaml"
    try:
        text = path.read_bytes()
    except OSError:
        return None, None, "installed-contract-unreadable", {}
    if hashlib.sha256(text).hexdigest() != contract.get("contract_content_hash"):
        return None, None, "installed-contract-hash-mismatch", {}
    data = yaml.safe_load(text)
    dispatch = data.get("runtime_dispatch", {}) if isinstance(data, dict) else {}
    if not isinstance(dispatch, dict):
        return None, None, "runtime-dispatch-invalid", {}
    invocation = dispatch.get("invocation")
    external = (
        (data.get("externally_produced_topics", []) or [])
        if isinstance(data, dict)
        else []
    )
    bindings = tuple(
        ModelCommandSeamExternalSender.model_validate(row) for row in external
    )
    return (
        dispatch.get("command_topic"),
        ModelCommandInvocation.model_validate(invocation)
        if invocation is not None
        else None,
        None,
        {row.topic: row.producer for row in bindings},
    )


async def load_runtime_inventory(
    overlay: ModelCommandSeamOverlay,
) -> tuple[ModelCommandSeamNode, ...]:
    """No partial manifest is returned as complete when an endpoint is unreadable."""
    async with httpx.AsyncClient(timeout=overlay.request_timeout_seconds) as client:
        responses = await asyncio.gather(
            *(client.get(url) for url in overlay.manifest_urls)
        )
    nodes: dict[tuple[str, str], ModelCommandSeamNode] = {}
    for response in responses:
        response.raise_for_status()
        document = response.json()
        if document.get("errors"):
            raise ValueError("runtime-manifest-discovery-errors")
        contracts = document.get("contracts")
        if not isinstance(contracts, list) or not contracts:
            raise ValueError("runtime-manifest-empty-or-invalid")
        for contract in contracts:
            bus = contract.get("event_bus") or {}
            command, invocation, error, external = _installed_binding(contract, overlay)
            node = ModelCommandSeamNode(
                node=contract["name"],
                package=contract["package_name"],
                subscribes=tuple(bus.get("subscribe_topics", ())),
                publishes=tuple(
                    sorted(
                        set(bus.get("publish_topics", ()))
                        | set(contract.get("compatibility_publish_topics", ()))
                    )
                ),
                command_topic=command,
                invocation=invocation,
                external_senders=external,
                binding_error=error,
            )
            key = (node.package, node.node)
            if key in nodes and nodes[key] != node:
                raise ValueError("runtime-manifest-contract-conflict")
            nodes[key] = node
    return tuple(nodes[key] for key in sorted(nodes))


async def load_topic_last_crossings(
    overlay: ModelCommandSeamOverlay,
) -> dict[str, datetime]:
    """Reuse the existing projection for last-known timestamps after retention/restart.

    A complete cursor walk is required. Old rows are historical positive
    observations, never evidence of a continuously observed negative window.
    """
    newest: dict[str, datetime] = {}
    cursor: object = None
    async with httpx.AsyncClient(timeout=overlay.request_timeout_seconds) as client:
        for _ in range(overlay.max_projection_pages):
            params = {"limit": "500"}
            if cursor is not None:
                params["since"] = str(cursor)
            response = await client.get(overlay.projection_url, params=params)
            response.raise_for_status()
            document = response.json()
            rows = document.get("rows")
            if not isinstance(rows, list):
                raise ValueError("activity-projection-rows-invalid")
            for row in rows:
                stamp = row.get("newest_message_at")
                if stamp is None:
                    continue
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("activity-projection-timestamp-without-zone")
                newest[row["topic"]] = parsed
            if document.get("truncated") is False:
                return newest
            next_cursor = document.get("next_cursor")
            if (
                document.get("truncated") is not True
                or next_cursor is None
                or next_cursor == cursor
            ):
                raise ValueError("activity-projection-cursor-invalid")
            cursor = next_cursor
    raise ValueError("activity-projection-page-bound-exceeded")
