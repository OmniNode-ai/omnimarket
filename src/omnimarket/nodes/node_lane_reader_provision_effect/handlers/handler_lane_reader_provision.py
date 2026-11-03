# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provision the inputs of ``onex-capability-catalog find`` and the ledger parity check on one lane host.

A lane on a lab host could run neither reader: the catalog CLI answered "no catalog.json given" because
``ONEX_CAPABILITY_CATALOG_JSON`` was set nowhere, the CLI was not on the lane PATH, and the parity check had no lab env file named
for it (``WL_ENV_FILE``). Lanes placed on the host by a lab run inherit the environment file of the host's lab-work serve unit, so this
effect (1) produces ``catalog.json`` where the committed table declares it, (2) links the catalog CLI into the lane PATH directory,
and (3) sets the two variables in the unit's environment file, rewriting only those two lines. It names, without a value, what the
host owner must supply: the database password key inside the lab env file. It reads key NAMES from that file and never a value.

Hosts, paths and commands come from the overlay and the committed table; this module names no host.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from ..models.model_lane_reader_provision import (
    ModelLaneReaderProvisionHost,
    ModelLaneReaderProvisionOverlay,
    ModelLaneReaderProvisionRequest,
    ModelLaneReaderProvisionResult,
    ModelLaneReaderProvisionStep,
    ModelLaneReaderTableRow,
    StepState,
)
from ..protocols import ProtocolLaneReaderHost
from .adapter_host_files import HostFiles, load_overlay, load_table

CATALOG_ENV = "ONEX_CAPABILITY_CATALOG_JSON"
WL_ENV = "WL_ENV_FILE"
CATALOG_CLI = "onex-capability-catalog"
# The key the parity check builds its database DSN from; the host owner supplies it, this effect never holds a value.
WL_PASSWORD_KEY = "POSTGRES_PASSWORD"

# A value is written unquoted into a systemd EnvironmentFile line.
_SAFE_VALUE = re.compile(r"^/[A-Za-z0-9_@+=:,./~-]+$")
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def env_key_names(text: str) -> frozenset[str]:
    """The variable NAMES an env file assigns; values are never kept."""
    return frozenset(
        m.group(1) for line in text.splitlines() if (m := _KEY_LINE.match(line))
    )


def upsert_env_lines(text: str, assignments: Sequence[tuple[str, str]]) -> str:
    """Set each ``NAME=value``: replace the line that assigns the name, else append it; every other line is kept as it is."""
    lines = text.splitlines()
    for name, value in assignments:
        line = f"{name}={value}"
        positions = [
            i
            for i, existing in enumerate(lines)
            if (m := _KEY_LINE.match(existing)) and m.group(1) == name
        ]
        if positions:
            lines[positions[0]] = line
            for i in reversed(positions[1:]):
                del lines[i]
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


class HandlerLaneReaderProvision:
    """``handle(request) -> result`` with injectable host dependencies.

    Runtime auto-wiring constructs the handler before host deployment values
    are needed. Without injected values, load the overlay and table only when
    handling a request; construction performs no host reads or writes.
    """

    def __init__(
        self,
        overlay: ModelLaneReaderProvisionOverlay | None = None,
        table: Mapping[str, ModelLaneReaderTableRow] | None = None,
        port: ProtocolLaneReaderHost | None = None,
    ) -> None:
        self._overlay = overlay
        self._table = table
        self._port = port if port is not None else HostFiles()

    def handle(
        self, request: ModelLaneReaderProvisionRequest
    ) -> ModelLaneReaderProvisionResult:
        steps: list[ModelLaneReaderProvisionStep] = []
        operator: list[str] = []
        overlay = self._overlay if self._overlay is not None else load_overlay()
        table = self._table if self._table is not None else load_table()
        host = next((h for h in overlay.hosts if h.name == request.host), None)
        row = table.get(request.host)
        if host is None or row is None:
            missing = "the host overlay" if host is None else "the committed host table"
            steps.append(
                _step(
                    "declaration",
                    "refused",
                    f"host {request.host!r} is not declared in {missing}",
                )
            )
            return self._result(request, steps, operator, restart_needed=False)

        catalog = f"{host.home}/{row.catalog_json}"
        wl_env = f"{host.home}/{row.wl_env_file}"
        bad = [p for p in (catalog, wl_env) if not _SAFE_VALUE.match(p)]
        if bad:
            steps.append(
                _step(
                    "declaration",
                    "refused",
                    f"a path holds a character a unit line cannot carry unquoted: {bad}",
                )
            )
            return self._result(request, steps, operator, restart_needed=False)

        steps.append(self._catalog_json(request, host, catalog))
        steps.append(self._cli_link(request, host))
        env_step, changed = self._serve_env(request, host, catalog, wl_env)
        steps.append(env_step)
        steps.append(self._wl_env_keys(host, wl_env, operator))
        restart_needed = changed and not request.check_only
        if request.restart and restart_needed:
            steps.append(self._restart(host))
            restart_needed = steps[-1].state != "changed"
        return self._result(request, steps, operator, restart_needed=restart_needed)

    def _result(
        self,
        request: ModelLaneReaderProvisionRequest,
        steps: list[ModelLaneReaderProvisionStep],
        operator: list[str],
        *,
        restart_needed: bool,
    ) -> ModelLaneReaderProvisionResult:
        return ModelLaneReaderProvisionResult(
            host=request.host,
            steps=tuple(steps),
            restart_needed=restart_needed,
            operator_steps=tuple(operator),
            correlation_id=request.correlation_id,
        )

    def _catalog_json(
        self,
        request: ModelLaneReaderProvisionRequest,
        host: ModelLaneReaderProvisionHost,
        catalog: str,
    ) -> ModelLaneReaderProvisionStep:
        if self._port.exists(catalog):
            return _step("catalog-json", "unchanged", f"{catalog} is present")
        if request.check_only:
            return _step(
                "catalog-json",
                "would-change",
                f"{catalog} is absent; the producer would run",
            )
        argv = [
            part.replace("{catalog_json}", catalog).replace(
                "{catalog_dir}", str(PurePosixPath(catalog).parent)
            )
            for part in host.catalog_producer_argv
        ]
        status, tail = self._port.run(
            argv, {"HOME": host.home, "OMNI_HOME": host.registry_root}
        )
        if status != 0:
            return _step(
                "catalog-json",
                "failed",
                f"the catalog producer exited {status}: {tail}",
            )
        if not self._port.exists(catalog):
            return _step(
                "catalog-json",
                "failed",
                f"the catalog producer exited 0 but {catalog} is absent",
            )
        return _step("catalog-json", "changed", f"{catalog} produced")

    def _cli_link(
        self,
        request: ModelLaneReaderProvisionRequest,
        host: ModelLaneReaderProvisionHost,
    ) -> ModelLaneReaderProvisionStep:
        link = f"{host.lane_bin_dir}/{CATALOG_CLI}"
        if not self._port.exists(host.catalog_cli_target):
            return _step(
                "catalog-cli",
                "failed",
                f"the declared target {host.catalog_cli_target} is absent",
            )
        current = self._port.link_target(link)
        if current == host.catalog_cli_target:
            return _step(
                "catalog-cli", "unchanged", f"{link} points at the declared target"
            )
        if current is None and self._port.exists(link):
            return _step(
                "catalog-cli", "refused", f"{link} is not a link; it is left as it is"
            )
        if request.check_only:
            return _step(
                "catalog-cli",
                "would-change",
                f"{link} would point at the declared target",
            )
        self._port.replace_link(link, host.catalog_cli_target)
        return _step("catalog-cli", "changed", f"{link} points at the declared target")

    def _serve_env(
        self,
        request: ModelLaneReaderProvisionRequest,
        host: ModelLaneReaderProvisionHost,
        catalog: str,
        wl_env: str,
    ) -> tuple[ModelLaneReaderProvisionStep, bool]:
        text = self._port.read_text(host.serve_env_file)
        if text is None:
            return (
                _step(
                    "serve-env",
                    "refused",
                    f"{host.serve_env_file} is absent; the host installer writes it first",
                ),
                False,
            )
        updated = upsert_env_lines(text, [(CATALOG_ENV, catalog), (WL_ENV, wl_env)])
        if updated == (text if text.endswith("\n") else text + "\n"):
            return _step(
                "serve-env", "unchanged", f"{CATALOG_ENV} and {WL_ENV} are set"
            ), False
        if request.check_only:
            return _step(
                "serve-env", "would-change", f"{CATALOG_ENV} and {WL_ENV} would be set"
            ), True
        self._port.write_text_atomic(host.serve_env_file, updated)
        return _step(
            "serve-env",
            "changed",
            f"{CATALOG_ENV} and {WL_ENV} set in {host.serve_env_file}",
        ), True

    def _wl_env_keys(
        self, host: ModelLaneReaderProvisionHost, wl_env: str, operator: list[str]
    ) -> ModelLaneReaderProvisionStep:
        text = self._port.read_text(wl_env)
        owner = f"host owner of {host.name} supplies the key {WL_PASSWORD_KEY} in {wl_env} (this node never holds a value)"
        if text is None:
            operator.append(owner)
            return _step(
                "wl-env-file", "needs-operator", f"{wl_env} is absent; {owner}"
            )
        if WL_PASSWORD_KEY not in env_key_names(text):
            operator.append(owner)
            return _step(
                "wl-env-file",
                "needs-operator",
                f"{wl_env} holds no {WL_PASSWORD_KEY} key; {owner}",
            )
        return _step(
            "wl-env-file", "unchanged", f"{wl_env} holds the key {WL_PASSWORD_KEY}"
        )

    def _restart(
        self, host: ModelLaneReaderProvisionHost
    ) -> ModelLaneReaderProvisionStep:
        if not host.restart_argv:
            return _step(
                "restart",
                "refused",
                "the overlay declares no restart_argv for the host",
            )
        status, tail = self._port.run(host.restart_argv, {"HOME": host.home})
        if status != 0:
            return _step("restart", "failed", f"the restart exited {status}: {tail}")
        return _step(
            "restart",
            "changed",
            "the serve unit restarted and re-reads its environment file",
        )


def _step(step: str, state: StepState, detail: str) -> ModelLaneReaderProvisionStep:
    return ModelLaneReaderProvisionStep(step=step, state=state, detail=detail)
