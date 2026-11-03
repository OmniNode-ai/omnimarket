# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe and repair the declared lane toolchain on one lab host (OMN-22414).

Lanes placed on a lab host tripped on a half-installed toolchain: the bare ``onex`` on PATH raised ``ModuleNotFoundError``,
pyright-python's nodeenv directory was empty and its install exited 1, and two hosts had no node or pnpm. For a declared host this
effect probes each tool the committed table lists for it, under the PATH lanes run with, and repairs a failing one the way the
table declares: it links the sanctioned wrapper into the lane PATH directory, removes an empty cache directory the tool will
recreate, or runs the installer argv the host overlay declares. It re-probes after a repair and reports the tool as changed only
when the probe then passes. A tool whose installer the overlay does not declare is named as an owner step.

Hosts, paths, tools and commands come from the overlay and the committed table; this module names no host and no tool.
"""

from __future__ import annotations

from omnimarket.models.lane_toolchain import (
    ModelLaneToolchainTable,
    ModelToolchainTool,
)

from ..models.model_lane_toolchain_provision import (
    ModelLaneToolchainProvisionHost,
    ModelLaneToolchainProvisionOverlay,
    ModelLaneToolchainProvisionRequest,
    ModelLaneToolchainProvisionResult,
    ModelLaneToolchainProvisionStep,
    StepState,
)
from ..protocols import ProtocolLaneToolchainHost
from .adapter_host_files import HostFiles, load_overlay, load_table


def _step(tool: str, state: StepState, detail: str) -> ModelLaneToolchainProvisionStep:
    return ModelLaneToolchainProvisionStep(
        step=f"tool:{tool}" if tool else "declaration", state=state, detail=detail
    )


class HandlerLaneToolchainProvision:
    """``handle(request) -> result`` with injectable host dependencies.

    Runtime auto-wiring constructs the handler before host deployment values are needed. Without injected values, load the
    overlay and table only when handling a request; construction performs no host reads or writes.
    """

    def __init__(
        self,
        overlay: ModelLaneToolchainProvisionOverlay | None = None,
        table: ModelLaneToolchainTable | None = None,
        port: ProtocolLaneToolchainHost | None = None,
    ) -> None:
        self._overlay = overlay
        self._table = table
        self._port = port if port is not None else HostFiles()

    def handle(
        self, request: ModelLaneToolchainProvisionRequest
    ) -> ModelLaneToolchainProvisionResult:
        overlay = self._overlay if self._overlay is not None else load_overlay()
        table = self._table if self._table is not None else load_table()
        host = next((h for h in overlay.hosts if h.name == request.host), None)
        tools = table.hosts.get(request.host)
        steps: list[ModelLaneToolchainProvisionStep] = []
        operator: list[str] = []
        if host is None or tools is None:
            missing = "the host overlay" if host is None else "the committed table"
            steps.append(
                _step(
                    "", "refused", f"host {request.host!r} is not declared in {missing}"
                )
            )
        else:
            for name in tools:
                steps.append(
                    self._tool(request, host, name, table.tools[name], operator)
                )
        return ModelLaneToolchainProvisionResult(
            host=request.host,
            steps=tuple(steps),
            operator_steps=tuple(operator),
            correlation_id=request.correlation_id,
        )

    def _probe(
        self, host: ModelLaneToolchainProvisionHost, spec: ModelToolchainTool
    ) -> tuple[bool, str]:
        status, tail = self._port.run(
            spec.probe,
            {
                "HOME": host.home,
                "PATH": host.lane_path,
                "OMNI_HOME": host.registry_root,
            },
        )
        # A lab run once printed "command not found" and exited 0: the words count as much as the status.
        working = status == 0 and "command not found" not in tail.lower()
        return working, f"`{' '.join(spec.probe)}` exit {status}: {tail}"

    def _tool(
        self,
        request: ModelLaneToolchainProvisionRequest,
        host: ModelLaneToolchainProvisionHost,
        name: str,
        spec: ModelToolchainTool,
        operator: list[str],
    ) -> ModelLaneToolchainProvisionStep:
        working, reading = self._probe(host, spec)
        if working:
            return _step(name, "unchanged", reading)

        def owner(why: str) -> ModelLaneToolchainProvisionStep:
            operator.append(
                f"host owner of {host.name}: {name}: {why}; {spec.owner_fix}"
            )
            return _step(name, "needs-operator", f"{reading}; {why}; {spec.owner_fix}")

        repair = spec.repair
        action: str
        if repair.kind == "lane-link":
            target = f"{host.registry_root}/{repair.target}"
            link = f"{host.lane_bin_dir}/{name}"
            if not self._port.exists(target):
                return _step(
                    name, "failed", f"{reading}; the declared target {target} is absent"
                )
            current = self._port.link_target(link)
            if current == target:
                return owner(f"{link} already points at the declared target")
            if current is None and self._port.exists(link):
                return _step(
                    name,
                    "refused",
                    f"{reading}; {link} is not a link and is left as it is",
                )
            action = f"{link} would point at {target}"
        elif repair.kind == "clear-empty-tree":
            directory = f"{host.home}/{repair.path}"
            held = self._port.file_count(directory)
            if held != 0:
                why = "is absent" if held is None else f"holds {held} file(s)"
                return owner(f"{directory} {why}, so there is no empty tree to clear")
            action = f"{directory} would be removed"
        else:
            argv = host.install.get(name)
            if not argv:
                return owner("the host overlay declares no installer argv for the tool")
            action = f"`{' '.join(argv)}` would run"

        if request.check_only:
            return _step(name, "would-change", f"{reading}; {action}")
        failure = self._apply(host, name, spec)
        if failure is not None:
            return _step(name, "failed", f"{reading}; {failure}")
        working, after = self._probe(host, spec)
        if not working:
            return _step(
                name, "failed", f"repaired, but the probe still fails: {after}"
            )
        return _step(name, "changed", f"{action.replace(' would ', ' ')}; then {after}")

    def _apply(
        self,
        host: ModelLaneToolchainProvisionHost,
        name: str,
        spec: ModelToolchainTool,
    ) -> str | None:
        """Make the repair; the failure text, or ``None`` when it was made."""
        repair = spec.repair
        if repair.kind == "lane-link":
            self._port.replace_link(
                f"{host.lane_bin_dir}/{name}", f"{host.registry_root}/{repair.target}"
            )
        elif repair.kind == "clear-empty-tree":
            self._port.remove_empty_tree(f"{host.home}/{repair.path}")
        else:
            argv = host.install[name]
            status, tail = self._port.run(
                argv, {"HOME": host.home, "PATH": host.lane_path}
            )
            if status != 0:
                return f"the installer exited {status}: {tail}"
        return None
