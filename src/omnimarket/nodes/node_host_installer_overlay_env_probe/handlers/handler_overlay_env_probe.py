# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay environment probe: every name a node's overlay declares, as set or unset on each host that runs the node.

The overlay is found by the shared reader (``find_node_overlay``, the roots in order) and read as YAML like the other
host installer nodes read theirs. Which keys declare a name is the contract's ``probe`` block, never this code: a key
whose last segment ends with one of ``name_key_suffixes`` and whose value matches ``name_pattern`` declares a name. The
node's own ``contract.yaml`` blocks named in ``contract_blocks`` are read the same way (the readiness host of the R8
scenario is declared there). A host with an unset name for a node it runs is unavailable for that node; one alarm per
host, node and name; placement verdicts for that node are rewritten ineligible for such a host. No value is read,
carried or printed: set-ness arrives as names.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.models.model_overlay_env_probe import (
    ModelEnvAlarm,
    ModelEnvNameRow,
    ModelNodeUnavailable,
    ModelOverlayEnvProbeRequest,
    ModelOverlayEnvProbeResult,
    ModelProbeHost,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_placement import (
    ModelHostTableVerdict,
)

NODE_PACKAGE = "omnimarket.nodes.node_host_installer_overlay_env_probe"
NODES_PACKAGE = "omnimarket.nodes"


class ProbeRulesError(ValueError):
    """The probe block of the contract is missing or invalid, so nothing can be judged."""


def load_probe_rules() -> dict[str, Any]:
    """Read ``probe`` from this node's ``contract.yaml``."""
    text = resources.files(NODE_PACKAGE).joinpath("contract.yaml").read_text()
    block = (yaml.safe_load(text) or {}).get("probe")
    if not isinstance(block, dict):
        raise ProbeRulesError("the overlay env probe contract declares no probe rules")
    for key in ("name_key_suffixes", "name_pattern", "contract_blocks"):
        if key not in block:
            raise ProbeRulesError(f"the probe rules declare no {key}")
    return block


def set_names_in(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The names of ``environ`` whose value is not blank, sorted. Values are dropped here, never carried on."""
    return tuple(sorted(key for key, value in environ.items() if value.strip()))


def _declared(
    document: object, suffixes: tuple[str, ...], pattern: re.Pattern[str], prefix: str
) -> list[tuple[str, str]]:
    """(key path, name) for each declaring key of a nested document, in document order."""
    found: list[tuple[str, str]] = []
    if isinstance(document, dict):
        for key, value in document.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if (
                isinstance(value, str)
                and any(str(key).endswith(suffix) for suffix in suffixes)
                and pattern.fullmatch(value.strip())
            ):
                found.append((path, value.strip()))
            else:
                found.extend(_declared(value, suffixes, pattern, path))
    elif isinstance(document, list):
        for index, item in enumerate(document):
            found.extend(_declared(item, suffixes, pattern, f"{prefix}[{index}]"))
    return found


def _read_overlay(node: str, roots: tuple[str, ...]) -> object:
    path = find_node_overlay(node, {OVERLAY_ROOTS_ENV: os.pathsep.join(roots)})
    if path is None:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise NodeOverlayError(
            f"overlay of {node} is unreadable ({type(exc).__name__})"
        ) from None


def _read_contract_blocks(node: str, blocks: tuple[str, ...]) -> dict[str, object]:
    try:
        text = (
            resources.files(NODES_PACKAGE)
            .joinpath(node, "contract.yaml")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, NotADirectoryError):
        return {}
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        return {}
    return {block: document[block] for block in blocks if block in document}


class HandlerOverlayEnvProbe:
    """EFFECT handler: read each assigned node's declared names and judge them against each host's set names."""

    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self._rules = rules or load_probe_rules()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def declarations(
        self, node: str, roots: tuple[str, ...]
    ) -> list[tuple[Literal["overlay", "contract"], str, str]]:
        """(source, key path, name) the node declares, de-duplicated by name (the overlay wins)."""
        suffixes = tuple(self._rules["name_key_suffixes"])
        pattern = re.compile(str(self._rules["name_pattern"]))
        out: list[tuple[Literal["overlay", "contract"], str, str]] = []
        seen: set[str] = set()
        overlay = _read_overlay(node, roots)
        sources: list[tuple[Literal["overlay", "contract"], object]] = [
            ("overlay", overlay)
        ]
        contract = _read_contract_blocks(node, tuple(self._rules["contract_blocks"]))
        sources.append(("contract", contract))
        for source, document in sources:
            for path, name in _declared(document, suffixes, pattern, ""):
                if name not in seen:
                    seen.add(name)
                    out.append((source, path, name))
        return out

    def handle(
        self, request: ModelOverlayEnvProbeRequest
    ) -> ModelOverlayEnvProbeResult:
        roots = tuple(Path(root).as_posix() for root in request.overlay_roots)
        rows: list[ModelEnvNameRow] = []
        alarms: list[ModelEnvAlarm] = []
        unavailable: list[ModelNodeUnavailable] = []
        cache: dict[str, list[tuple[Literal["overlay", "contract"], str, str]]] = {}
        try:
            for host in request.hosts:
                for node in host.nodes:
                    if node not in cache:
                        cache[node] = self.declarations(node, roots)
                    unset = self._judge(host, node, cache[node], rows, alarms)
                    if unset:
                        unavailable.append(
                            ModelNodeUnavailable(
                                host_name=host.host_name,
                                node=node,
                                unset_names=tuple(unset),
                            )
                        )
        except NodeOverlayError as exc:
            return ModelOverlayEnvProbeResult(
                status="refused", exit_code=65, reason=str(exc)
            )
        verdicts: tuple[ModelHostTableVerdict, ...] = ()
        eligible: tuple[str, ...] = ()
        if request.placement is not None:
            verdicts = self._apply(
                request.placement.node, request.placement.verdicts, unavailable
            )
            eligible = tuple(v.host_name for v in verdicts if v.eligible)
        return ModelOverlayEnvProbeResult(
            status="success",
            rows=tuple(rows),
            alarms=tuple(alarms),
            unavailable=tuple(unavailable),
            verdicts=verdicts,
            eligible_hosts=eligible,
        )

    @staticmethod
    def _judge(
        host: ModelProbeHost,
        node: str,
        declared: list[tuple[Literal["overlay", "contract"], str, str]],
        rows: list[ModelEnvNameRow],
        alarms: list[ModelEnvAlarm],
    ) -> list[str]:
        have = set(host.set_names)
        unset: list[str] = []
        for source, path, name in declared:
            state: Literal["set", "unset"] = "set" if name in have else "unset"
            rows.append(
                ModelEnvNameRow(
                    host_name=host.host_name,
                    node=node,
                    name=name,
                    source=source,
                    declared_by=path,
                    state=state,
                )
            )
            if state == "unset":
                unset.append(name)
                alarms.append(
                    ModelEnvAlarm(
                        host_name=host.host_name,
                        node=node,
                        name=name,
                        message=f"host {host.host_name} lacks {name}, which node {node} declares",
                    )
                )
        return unset

    @staticmethod
    def _apply(
        node: str,
        verdicts: tuple[ModelHostTableVerdict, ...],
        unavailable: list[ModelNodeUnavailable],
    ) -> tuple[ModelHostTableVerdict, ...]:
        blocked = {u.host_name: u for u in unavailable if u.node == node}
        out: list[ModelHostTableVerdict] = []
        for verdict in verdicts:
            hit = blocked.get(verdict.host_name)
            if hit is not None and verdict.eligible:
                verdict = verdict.model_copy(
                    update={
                        "eligible": False,
                        "reason": f"unset overlay names for {node}: {', '.join(hit.unset_names)}",
                    }
                )
            out.append(verdict)
        return tuple(out)
